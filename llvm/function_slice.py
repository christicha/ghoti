import os
import json
import re
import subprocess
import shutil
from configparser import ConfigParser

from openai import OpenAI

# ==========================================
# 🌲 Tree-sitter AST 依赖检查
# ==========================================
try:
    from tree_sitter import Language, Parser
    import tree_sitter_cpp

    TREE_SITTER_AVAILABLE = True
except ImportError:
    TREE_SITTER_AVAILABLE = False
    print("⚠️ 警告：未检测到 tree-sitter 或 tree-sitter-cpp。AST 解析将无法工作，请先使用 pip 安装。")


# ==========================================
# 模块 1: AST 解析与覆盖率底层工具
# ==========================================
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


# ==========================================
# 模块 2: 单文件 AST 切片与过滤
# ==========================================
def function_slice(bugid, configPath, target_file_path):
    """
    单文件处理版：复制指定目标文件 -> AST 切割 -> 多维覆盖率交集过滤 -> 导出为 JSON 格式的 MD
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
    print(f"🌲 开始基于 AST 处理单文件: {os.path.basename(target_file_path)}")
    print(f"==============================================\n")

    if not TREE_SITTER_AVAILABLE:
        print("❌ 中止：无法执行 AST 解析。")
        return

    # 验证目标文件是否存在
    if not os.path.exists(target_file_path):
        print(f"❌ 找不到指定的目标文件: {target_file_path}")
        return

    # 准备工作目录并复制文件
    os.makedirs(fileListDir, exist_ok=True)
    filename = os.path.basename(target_file_path)
    local_file_path = os.path.join(fileListDir, filename)

    try:
        shutil.copy2(target_file_path, local_file_path)
        print(f"✅ 已将目标文件复制到工作区: {local_file_path}")
    except Exception as e:
        print(f"❌ 复制文件失败: {str(e)}")
        return

    # 1. 收集并计算覆盖率交集
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
        print("⚠️ 警告：交集为空，后续生成的 Markdown 将不包含任何命中的函数。")

    # 验证文件后缀
    if not (filename.endswith('.cpp') or filename.endswith('.c') or filename.endswith('.h')):
        print(f"⚠️ 警告: 目标文件不是 C/C++ 源码文件，跳过 AST 解析: {filename}")
        return

    # 2. 单文件 AST 切割并就地过滤
    all_ast_functions = extract_functions_with_ast(local_file_path)
    retained_blocks = []
    file_discarded = 0

    for func in all_ast_functions:
        ast_sig = func["signature"]
        if is_signature_match(ast_sig, common_executed_sigs):
            json_str = json.dumps(func, ensure_ascii=False, indent=2)
            retained_blocks.append(f"```json\n{json_str}\n```")
        else:
            file_discarded += 1

    # 3. 将命中的函数写出为 _filtered.md
    filtered_file_path = os.path.join(fileListDir, f"{filename}_filtered.md")
    with open(filtered_file_path, 'w', encoding='utf-8') as f:
        if retained_blocks:
            f.write("\n\n".join(retained_blocks))
        else:
            f.write("\n")

    print(f"📄 单文件切割完成: {filename}")
    print(f"   - AST 提取了 {len(all_ast_functions)} 个函数")
    print(f"   - ✅ 命中保留: {len(retained_blocks)} 个")
    print(f"   - 🗑️ 未执行扔掉: {file_discarded} 个")


# ==========================================
# 模块 3: 单文件 LLM 提纯摘要
# ==========================================
# ==========================================
# 模块 3: 单文件 LLM 提纯摘要 (修改版)
# ==========================================
def generate_function_summaries(bugid, configPath, target_file_path):
    """
    单文件提纯版：读取指定文件的 AST 切片，调用 LLM 生成摘要并过滤无效函数。
    保存两份：一份带函数体 (_with_body.md)，一份不带函数体 (_no_body.md)。
    """
    cfg = ConfigParser()
    cfg.read(configPath)

    baseFileListDir = cfg.get('llvm-locations', 'fileListDir')
    summaryDir = cfg.get('llvm-locations', 'summaryDir')
    fileListDir = os.path.join(baseFileListDir, bugid)

    api_key = cfg.get('deepseek', 'api')
    api_url = cfg.get('deepseek', 'url')

    orig_filename = os.path.basename(target_file_path)
    filtered_filename = f"{orig_filename}_filtered.md"
    input_file_path = os.path.join(fileListDir, filtered_filename)

    # 定义两个输出文件名
    output_path_with_body = os.path.join(fileListDir, f"{orig_filename}_with_body.md")
    output_path_no_body = os.path.join(fileListDir, f"{orig_filename}_no_body.md")

    print(f"\n==============================================")
    print(f"📝 开始提纯单文件切片: {filtered_filename}")
    print(f"==============================================\n")

    if not os.path.exists(input_file_path):
        print(f"❌ 找不到该文件的切片数据: {input_file_path}")
        return

    client = OpenAI(api_key=api_key, base_url=api_url, timeout=3600)

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
    with open(input_file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    json_blocks = re.findall(r'```json\n(.*?)\n```', content, re.DOTALL)
    functions_data = []
    for block in json_blocks:
        try:
            functions_data.append(json.loads(block))
        except json.JSONDecodeError:
            continue

    if not functions_data:
        print(f"⚠️ {orig_filename} 没有命中的函数需要分析。")
        return

    # 3. 组装 LLM 输入
    llm_input_funcs = []
    for i, func in enumerate(functions_data):
        llm_input_funcs.append({
            "id": i,
            "signature": func.get("signature", ""),
            "source_code": func.get("source_code", "")
        })

    llm_input_json = json.dumps(llm_input_funcs, ensure_ascii=False, indent=2)

    # 4. 提示词 (保持不变)
    system_prompt = (
        "你是一位资深的 C++ 逆向工程师与 LLVM 编译器架构专家。"
        "你擅长通过局部代码切片，精准推断函数的核心业务逻辑，并能一眼看穿哪些是毫无营养的死代码。"
    )

    user_prompt = f"""# 任务目标
    我正在进行编译器缺陷定位分析。以下是来自 LLVM 源码文件 `{orig_filename}` 中的核心执行函数切片。
    请阅读代码，执行【智能降噪过滤】，并为保留的核心函数生成【功能摘要】。

    # 上下文信息
    * **文件全局功能摘要**: {file_summary}

    # 待分析的函数代码切片
    {llm_input_json}

    # 分析与提取指令
    1. **智能过滤**：标记微不足道（Getter/Setter/调试等）的代码为 `is_trivial: true`。
    2. **功能提炼**：对重要函数（`is_trivial: false`）生成 1-3 句话的摘要。

    # 输出格式要求
    请仅输出合法的 JSON 数组。
    """

    # 5. 调用大模型
    try:
        response = client.chat.completions.create(
            model="deepseek-reasoner",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.1
        )

        reply_text = response.choices[0].message.content
        summary_match = re.search(r'```json\n(.*?)\n```', reply_text, re.DOTALL)
        summary_json_str = summary_match.group(1) if summary_match else reply_text.strip()
        summary_json_str = re.sub(r',\s*([\]}])', r'\1', summary_json_str)

        summaries = json.loads(summary_json_str)
        summary_dict = {item["id"]: item for item in summaries if "id" in item}

        # 6. 同时构建两份 Markdown 内容
        blocks_with_body = []
        blocks_no_body = []

        file_discarded = 0
        file_kept = 0

        for i, func in enumerate(functions_data):
            res = summary_dict.get(i, {"is_trivial": False, "summary": "⚠️ 摘要生成失败"})

            if res.get("is_trivial", False):
                file_discarded += 1
                continue

            file_kept += 1
            sig = func.get("signature", "")
            code = func.get("source_code", "")
            func_summary = res.get("summary", "")

            # 格式 A: 带函数体
            md_with_body = (
                f"### 📍 函数签名: `{sig}`\n\n"
                f"```cpp\n{code}\n```\n"
                f"> **💡 功能摘要**: {func_summary}\n\n---\n"
            )

            # 格式 B: 不带函数体
            md_no_body = (
                f"###函数签名: `{sig}`\n\n"
                f"> **功能摘要**: {func_summary}\n\n---\n"
            )

            blocks_with_body.append(md_with_body)
            blocks_no_body.append(md_no_body)

        # 7. 写入两个文件
        def write_file(path, blocks, title_suffix):
            with open(path, 'w', encoding='utf-8') as f:
                if blocks:
                    f.write(f"# {orig_filename} - {title_suffix}\n\n")
                    f.write("\n".join(blocks))
                else:
                    f.write("> **⚠️ 提示**：该文件内所有函数均已被滤除。\n")

        write_file(output_path_with_body, blocks_with_body, "详细逻辑版 (含代码)")
        write_file(output_path_no_body, blocks_no_body, "精简摘要版 (仅摘要)")

        print(f"   ✅ 单文件提纯完成！")
        print(f"      - 保存至: {os.path.basename(output_path_with_body)}")
        print(f"      - 保存至: {os.path.basename(output_path_no_body)}")
        print(f"      - 保留: {file_kept} | 剔除: {file_discarded}")

    except Exception as e:
        print(f"   ❌ 处理文件 {orig_filename} 时出错: {str(e)}")