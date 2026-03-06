import os
import json
import re
import subprocess
from configparser import ConfigParser

from openai import OpenAI

try:
    from tree_sitter import Language, Parser
    import tree_sitter_cpp

    TREE_SITTER_AVAILABLE = True
except ImportError:
    TREE_SITTER_AVAILABLE = False
    print("⚠️ 警告：未检测到 tree-sitter 或 tree-sitter-cpp。AST 解析将无法工作，请先使用 pip 安装。")


def extract_functions_with_ast(file_path):
    """
    使用 Tree-sitter AST 解析 C++ 源码，精准提取所有函数的签名和源码体
    """
    if not TREE_SITTER_AVAILABLE:
        return []

    with open(file_path, 'r', encoding='utf-8') as f:
        source_code = f.read()
    source_bytes = bytes(source_code, "utf8")

    # 初始化 C++ 解析器
    CPP_LANGUAGE = Language(tree_sitter_cpp.language())
    parser = Parser(CPP_LANGUAGE)
    tree = parser.parse(source_bytes)

    extracted_functions = []

    def traverse_tree(node):
        # 寻找函数定义节点 (function_definition)
        if node.type == 'function_definition':
            # 提取整个函数的完整源码
            func_text = source_bytes[node.start_byte:node.end_byte].decode('utf-8')

            # 提取函数签名 (即从起始位置到函数体 compound_statement 之前的内容)
            body_node = None
            for child in node.children:
                if child.type == 'compound_statement':
                    body_node = child
                    break

            if body_node:
                sig_text = source_bytes[node.start_byte:body_node.start_byte].decode('utf-8').strip()
            else:
                # Fallback: 按左大括号截断
                sig_text = func_text.split('{')[0].strip()

            # 清理签名中的换行和多余空格
            sig_text = re.sub(r'\s+', ' ', sig_text)

            extracted_functions.append({
                "signature": sig_text,
                "source_code": func_text
            })
        else:
            # 递归遍历子节点 (处理嵌套在 namespace 或 class 中的函数)
            for child in node.children:
                traverse_tree(child)

    # 从根节点开始遍历
    traverse_tree(tree.root_node)
    return extracted_functions


def get_executed_demangled_functions(method_info_path):
    executed_mangled_names = []
    if not os.path.exists(method_info_path):
        return set()
    with open(method_info_path, 'r', encoding='utf-8') as f:
        for line in f:
            parts = line.strip().split(',')
            if len(parts) >= 2:
                try:
                    if int(parts[-1].strip()) > 0:
                        executed_mangled_names.append(parts[1].strip())
                except ValueError:
                    continue
    if not executed_mangled_names: return set()
    try:
        process = subprocess.Popen(['c++filt'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)
        stdout, _ = process.communicate('\n'.join(executed_mangled_names))
        return {sig.strip() for sig in stdout.split('\n') if sig.strip()}
    except Exception:
        return set()


def is_signature_match(ast_signature, executed_demangled_sigs):
    match = re.search(r'([a-zA-Z0-9_~]+)\s*\(', ast_signature)
    if not match: return False
    core_func_name = match.group(1)

    namespace_match = re.search(r'([a-zA-Z0-9_]+)::' + re.escape(core_func_name), ast_signature)
    if namespace_match:
        core_func_name = f"{namespace_match.group(1)}::{core_func_name}"

    for exec_sig in executed_demangled_sigs:
        if core_func_name in exec_sig:
            return True
    return False


def function_slice(bugid, configPath):
    """
    全新升级版：本地 AST 切割 + 多维覆盖率交集过滤
    """
    cfg = ConfigParser()
    cfg.read(configPath)

    baseFileListDir = cfg.get('llvm-locations', 'fileListDir')
    baseInfoDir = cfg.get('llvm-locations', 'infodir')
    baseFailDir = cfg.get('llvm-locations', 'passdir')

    fileListDir = os.path.join(baseFileListDir, bugid)
    method_info = os.path.join(baseInfoDir, bugid, 'failcov', 'method_info.txt')
    failDir = os.path.join(baseFailDir, bugid, 'failcov')

    print(f"\n==============================================")
    print(f"🌲 开始基于 AST 语法感知切割与覆盖率【交集】过滤")
    print(f"==============================================\n")

    if not TREE_SITTER_AVAILABLE:
        print("❌ 中止：无法执行 AST 解析。")
        return

    # 1. 收集并计算覆盖率交集 (复用您优秀的交集逻辑)
    all_method_info_paths = []
    if os.path.exists(method_info):
        all_method_info_paths.append(method_info)
    if os.path.exists(failDir):
        for entry in os.listdir(failDir):
            sub_dir_path = os.path.join(failDir, entry)
            if os.path.isdir(sub_dir_path):
                sub_method_info = os.path.join(sub_dir_path, 'method_info.txt')
                if os.path.exists(sub_method_info):
                    all_method_info_paths.append(sub_method_info)

    common_executed_sigs = None
    for path in all_method_info_paths:
        sigs = get_executed_demangled_functions(path)
        if common_executed_sigs is None:
            common_executed_sigs = set(sigs)
        else:
            common_executed_sigs.intersection_update(sigs)
        if not common_executed_sigs:
            break

    if common_executed_sigs is None: common_executed_sigs = set()
    print(f"✅ 成功获取 {len(common_executed_sigs)} 个在【所有】失败用例中都执行过的核心函数签名。\n")

    if not common_executed_sigs:
        print("⚠️ 警告：交集为空，跳过过滤。")
        return

    # 2. 遍历源文件，AST 切割并就地过滤
    total_retained = 0
    total_discarded = 0

    # 我们直接找 cpp/c/h 文件进行解析，而不是去读之前大模型生成的 Markdown
    for filename in os.listdir(fileListDir):
        if not (filename.endswith('.cpp') or filename.endswith('.c') or filename.endswith('.h')):
            continue

        file_path = os.path.join(fileListDir, filename)

        # 使用 AST 提取出该文件所有的函数
        all_ast_functions = extract_functions_with_ast(file_path)

        retained_blocks = []
        file_discarded = 0

        for func in all_ast_functions:
            ast_sig = func["signature"]

            # 3. 核心交集比对
            if is_signature_match(ast_sig, common_executed_sigs):
                # 匹配成功，保存为 JSON 格式的 Markdown 块 (兼容你后续的 function_FL 逻辑)
                json_str = json.dumps(func, ensure_ascii=False, indent=2)
                retained_blocks.append(f"```json\n{json_str}\n```")
            else:
                file_discarded += 1

        total_retained += len(retained_blocks)
        total_discarded += file_discarded

        # 4. 将命中的函数写出为 _filtered.md
        filtered_file_path = os.path.join(fileListDir, f"{filename}_filtered.md")
        with open(filtered_file_path, 'w', encoding='utf-8') as f:
            if retained_blocks:
                f.write("\n\n".join(retained_blocks))
            else:
                f.write("\n")

        print(f"📄 文件: {filename}")
        print(f"   - AST 提取了 {len(all_ast_functions)} 个函数")
        print(f"   - ✅ 命中保留: {len(retained_blocks)} 个")
        print(f"   - 🗑️ 未执行扔掉: {file_discarded} 个")

    print(
        f"\n🎉 AST 语法切割清洗完成！全局共保留 {total_retained} 个硬核执行函数，精准丢弃 {total_discarded} 个未执行死代码。")


def generate_function_summaries(bugid, configPath):
    """
    读取 AST 过滤后的函数切片，调用 LLM 生成摘要并【过滤掉不重要的简单函数】。
    最终将保留下来的高价值函数重写为【C++代码块 + 摘要注释】的清爽 Markdown 格式。
    """
    cfg = ConfigParser()
    cfg.read(configPath)

    baseFileListDir = cfg.get('llvm-locations', 'fileListDir')
    summaryDir = cfg.get('llvm-locations', 'summaryDir')
    fileListDir = os.path.join(baseFileListDir, bugid)

    api_key = cfg.get('deepseek', 'api')
    api_url = cfg.get('deepseek', 'url')

    print(f"\n==============================================")
    print(f"📝 开始为切片函数生成摘要，并智能剔除简单/无效函数")
    print(f"==============================================\n")

    if not os.path.exists(fileListDir):
        print(f"❌ 找不到目录: {fileListDir}")
        return

    client = OpenAI(api_key=api_key, base_url=api_url, timeout=3600)
    processed_files = 0
    total_kept = 0
    total_discarded = 0

    for filename in os.listdir(fileListDir):
        if not filename.endswith('_filtered.md'):
            continue

        file_path = os.path.join(fileListDir, filename)
        orig_filename = filename.replace('_filtered.md', '')

        # 1. 获取该文件的全局功能摘要
        file_summary = "⚠️ 暂无该文件的全局功能摘要。"
        json_path = os.path.join(summaryDir, f"{orig_filename}.json")
        if os.path.exists(json_path):
            try:
                with open(json_path, 'r', encoding='utf-8') as jf:
                    data = json.load(jf)
                    file_summary = data.get("summary", file_summary)
            except Exception:
                pass

        # 2. 读取 _filtered.md 中的 JSON 切片
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        json_blocks = re.findall(r'```json\n(.*?)\n```', content, re.DOTALL)
        functions_data = []
        for block in json_blocks:
            try:
                functions_data.append(json.loads(block))
            except json.JSONDecodeError:
                continue

        if not functions_data:
            continue

        print(f"🔍 正在审视 {orig_filename} 中的 {len(functions_data)} 个执行函数...")

        # 3. 组装 LLM 输入 (带上 ID，方便严格映射)
        llm_input_funcs = []
        for i, func in enumerate(functions_data):
            llm_input_funcs.append({
                "id": i,
                "signature": func.get("signature", ""),
                "source_code": func.get("source_code", "")
            })

        llm_input_json = json.dumps(llm_input_funcs, ensure_ascii=False, indent=2)

        # 4. 组装强化的提示词（增加过滤指令）
        system_prompt = (
            "你是一位资深的 C++ 逆向工程师与 LLVM 编译器架构专家。"
            "你擅长通过局部代码切片，精准推断函数的核心业务逻辑，并能一眼看穿哪些是毫无营养的死代码。"
        )

        user_prompt = f"""# 任务目标
        我正在进行编译器缺陷定位分析。以下是来自 LLVM 源码文件 `{orig_filename}` 中的核心执行函数切片。
        为了更好地排查 Bug，请你阅读这些代码，执行【智能降噪过滤】，并为保留下来的核心函数生成【功能摘要】。
        
        # 上下文信息
        * **文件全局功能摘要**: {file_summary}
        
        # 待分析的函数代码切片
        {llm_input_json}
        
        # 分析与提取指令（必须严格遵守）
        1. **智能过滤 (Noise Reduction)**：严格判断该函数是否为“不可能出错的微不足道的代码”。例如：
           - 纯函数声明 (Declarations without bodies)
           - 极其简单的 Getter/Setter 方法
           - 无实质逻辑的构造/析构函数
           - 仅包含 `assert`, `print`, `dump` 等调试代码的函数
           👉 如果属于此类，请将其 `is_trivial` 标记为 `true`。
        2. **功能提炼 (Summarization)**：如果函数包含实质的业务逻辑（`is_trivial` 为 `false`），请结合“文件全局功能”，推断这个函数扮演的具体角色，并用 1-3 句话精准总结其功能。说明它：**输入了什么条件，内部做了什么核心判断/转换，输出了什么结果**。
        
        # 输出格式要求
        请**仅输出**一个合法的 JSON 数组，必须包含传入的所有 `id`，不要输出任何额外的解释性文字。格式如下：
        [
          {{
            "id": 0,
            "is_trivial": false,
            "summary": "你生成的该函数功能摘要"
          }},
          {{
            "id": 1,
            "is_trivial": true,
            "summary": "简单方法，无需摘要"
          }}
        ]
        
        ⚠️ 严格注意：
        1. 所有的键名必须使用双引号（"id", "is_trivial", "summary"）。
        2. 绝对不能在数组最后一个对象后添加逗号（尾随逗号）。
        3. 如果摘要内容中包含双引号，请务必使用反斜杠转义（如 \\"）。
        """
        # 5. 调用大模型
        try:
            response = client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.1,
                max_tokens=8192
            )

            reply_text = response.choices[0].message.content

            # 6. 解析大模型返回的摘要与判定结果
            summary_match = re.search(r'```json\n(.*?)\n```', reply_text, re.DOTALL)
            summary_json_str = summary_match.group(1) if summary_match else reply_text.strip().strip('`').replace(
                'json\n', '')

            # 清理尾随逗号防止 JSON 解析崩溃
            summary_json_str = re.sub(r',\s*([\]}])', r'\1', summary_json_str)

            try:
                summaries = json.loads(summary_json_str)
            except json.JSONDecodeError as jde:
                print(f"   ⚠️ JSON 解析失败，大模型返回了不规范的格式。正在跳过此文件...")
                print(f"   [Debug] 错误的 JSON 片段: {summary_json_str[:100]}...")
                continue

                # 构建 ID 到解析结果的映射字典
            summary_dict = {
                item["id"]: {
                    "summary": item.get("summary", "⚠️ 摘要生成失败"),
                    "is_trivial": item.get("is_trivial", False)
                }
                for item in summaries if "id" in item
            }

            # 7. 重写 _filtered.md 文件：剔除 Trivial 函数，重构保留函数的 Markdown 排版
            new_markdown_blocks = []
            file_discarded = 0
            file_kept = 0

            for i, func in enumerate(functions_data):
                analysis_result = summary_dict.get(i, {"is_trivial": False, "summary": "⚠️ 未生成摘要"})

                # 【核心拦截逻辑】：如果是无关紧要的函数，直接抛弃！
                if analysis_result["is_trivial"]:
                    file_discarded += 1
                    continue

                # 保留函数的处理逻辑
                file_kept += 1
                sig = func.get("signature", "")
                code = func.get("source_code", "")
                func_summary = analysis_result["summary"]

                md_block = (
                    f"### 📍 函数签名: `{sig}`\n\n"
                    f"```cpp\n"
                    f"{code}\n"
                    f"```\n"
                    f"> **💡 功能摘要**: {func_summary}\n\n"
                    f"---\n"
                )
                new_markdown_blocks.append(md_block)

            # 覆盖原切片文件
            with open(file_path, 'w', encoding='utf-8') as f:
                if new_markdown_blocks:
                    f.write("\n".join(new_markdown_blocks))
                else:
                    f.write("> **⚠️ 提示**：该文件内所有覆盖到的函数均已被 LLM 判定为无关紧要的简单代码，已被全部滤除。\n")

            processed_files += 1
            total_kept += file_kept
            total_discarded += file_discarded

            print(f"   ✅ 完成！保留核心: {file_kept} 个 | 智能剔除无用: {file_discarded} 个")

        except Exception as e:
            print(f"   ❌ 处理文件 {filename} 时发生错误: {str(e)}")

    print(f"\n🎉 全链路提纯完成！共处理 {processed_files} 个文件。")
    print(f"   - 💡 提炼保留了 {total_kept} 个高危业务函数")
    print(f"   - 🗑️ 成功阻击了 {total_discarded} 个无效干扰函数")
