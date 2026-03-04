import json
import os
import re
import subprocess
from configparser import ConfigParser
from openai import OpenAI


def function_list(bugid,  configPath):
    cfg = ConfigParser()
    cfg.read(configPath)
    baseFileListDir = cfg.get('llvm-locations', 'fileListDir')
    fileListDir = os.path.join(baseFileListDir, bugid)

    # 获取 LLM API 配置
    api_key = cfg.get('deepseek', 'api')
    api_url = cfg.get('deepseek', 'url')

    if not os.path.exists(fileListDir):
        print(f"❌ 致命错误：找不到目标文件目录: {fileListDir}")
        return

    # 初始化 OpenAI 客户端
    client = OpenAI(api_key=api_key, base_url=api_url, timeout=3600)

    print(f"\n==============================================")
    print(f"🚀 开始为 BugID: {bugid} 执行函数级静态分析")
    print(f"==============================================\n")

    # 遍历目录下的源文件
    processed_count = 0
    for filename in os.listdir(fileListDir):
        # 过滤掉非源码文件和之前生成的输出文件
        if not (filename.endswith('.cpp') or filename.endswith('.c') or filename.endswith('.h')):
            continue

        file_path = os.path.join(fileListDir, filename)

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                source_code = f.read()
        except Exception as e:
            print(f"⚠️ 无法读取文件 {filename}: {str(e)}")
            continue

        print(f"🔍 正在提炼 {filename} 中的核心业务函数...")

        system_prompt = (
            "你是一位资深的 C++ 逆向工程师与 LLVM 编译器架构专家。"
            "你擅长进行静态代码分析，能够从庞大复杂的源码文件中精准剥离出核心的业务逻辑。"
        )

        # 使用双大括号 {{ 和 }} 来转义 f-string 中的 JSON 括号
        user_prompt = f"""# 任务目标
        我正在进行编译器缺陷定位（Fault Localization）。我将为你提供一个被标记为高危嫌疑的 C++ 源文件代码。
        请你对该文件进行深度静态解析，提取出其中所有的“可执行函数”，并为每个函数生成功能摘要。这些摘要将用于后续与 Bug 触发的特征进行比对，以锁定最终的缺陷代码行。
        
        # 输入数据
        {source_code}
        
        # 分析与提取指令（请严格遵守）
        1. 精准过滤 (Noise Reduction)：
        * 忽略纯函数声明（Declarations without definitions）。
        * 忽略简单的 Getter/Setter 方法、纯虚函数占位符、无实质逻辑的构造/析构函数。
        * 忽略用于打印调试信息的 dump() 或 print() 方法。
        * 重点关注包含复杂控制流、IR 指令转换、状态机更新、算术逻辑运算的核心业务函数（例如 LLVM 中的 runOnFunction、各种 visit... 方法、computeKnownBits... 或是底层的 Lower... 转换函数）。
        2. 提取内容特征 (Feature Extraction)：
        * 准确提取完整的函数签名。
        * 根据函数的内部逻辑，用 1-3 句话精准总结该函数的实际功能（重点说明它输入了什么、修改了什么状态、输出了什么）。
    
        # 输出格式要求
        为了便于自动化脚本解析，对于你提取出的每一个函数，请单独使用一个 Markdown JSON 代码框进行输出。
        绝对不要将所有函数合并成一个大的 JSON 数组，也不要输出任何额外的解释性废话或过渡性文字。请严格按照以下格式逐个输出：
        {{
          "signature": "完整的函数签名，如 bool llvm::InstCombiner::visitAdd(BinaryOperator &I)",
          "summary": "功能摘要，例如：处理加法指令的优化，尝试将常量加法折叠或进行代数化简。"
        }}
        """
        try:
            # 独立请求，不携带上下文历史
            response = client.chat.completions.create(
                model="deepseek-reasoner",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.1,
                max_tokens=50000
            )

            result_text = response.choices[0].message.content

            # 将大模型输出的 JSON 块集合保存为同名的 .md 文件
            out_file_path = os.path.join(fileListDir, f"{filename}_functions.md")
            with open(out_file_path, 'w', encoding='utf-8') as out_f:
                out_f.write(result_text)

            processed_count += 1
            print(f"  ✅ 成功！已提取并保存至: {out_file_path}")

        except Exception as e:
            print(f"  ❌ API 调用失败 [{filename}]: {str(e)}")

    print(f"\n🎉 函数级分析完成！共成功处理 {processed_count} 个文件。")


def get_executed_demangled_functions(method_info_path):
    """
    解析 coverage 文件，提取被执行的函数，并使用 c++filt 批量还原修饰名
    假设 method_info.txt 的格式类似: filename, mangled_name, ..., call_count
    """
    executed_mangled_names = []

    if not os.path.exists(method_info_path):
        print(f"⚠️ 找不到覆盖率文件: {method_info_path}")
        return set()

    with open(method_info_path, 'r', encoding='utf-8') as f:
        for line in f:
            parts = line.strip().split(',')
            if len(parts) >= 2:
                try:
                    # 假设最后一列是调用次数，大于 0 则表示被执行
                    call_count = int(parts[-1].strip())
                    if call_count > 0:
                        # 假设第二列是函数修饰名 (Mangled Name)
                        mangled_name = parts[1].strip()
                        executed_mangled_names.append(mangled_name)
                except ValueError:
                    continue

    if not executed_mangled_names:
        return set()

    # 使用 c++filt 批量将 mangled_names 转换为人类可读的签名
    try:
        process = subprocess.Popen(
            ['c++filt'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        # 将所有修饰名用换行符拼接传入 c++filt
        stdout, _ = process.communicate('\n'.join(executed_mangled_names))

        # 返回还原后的签名集合
        demangled_signatures = {sig.strip() for sig in stdout.split('\n') if sig.strip()}
        return demangled_signatures
    except Exception as e:
        print(f"❌ 执行 c++filt 失败 (请确保系统已安装 binutils): {str(e)}")
        return set()


def is_signature_match(llm_signature, executed_demangled_sigs):
    """
    模糊匹配逻辑：判断 LLM 提取的 C++ 签名是否在执行过的函数集合中。
    由于 LLM 提取的签名和 c++filt 还原的签名在空格、返回类型、const 上可能存在细微差别，
    这里采用提取核心“函数名”的容错匹配策略。
    """
    # 提取 LLM 签名中紧挨着左括号 '(' 前的核心函数名
    # 例如: "bool llvm::InstCombiner::visitAdd(BinaryOperator &I)" -> "visitAdd"
    match = re.search(r'([a-zA-Z0-9_~]+)\s*\(', llm_signature)

    if not match:
        return False

    core_func_name = match.group(1)

    # 为了避免重名函数误判，可以连带类名一起检查 (如果提取的签名包含类名)
    namespace_match = re.search(r'([a-zA-Z0-9_]+)::' + re.escape(core_func_name), llm_signature)
    if namespace_match:
        core_func_name = f"{namespace_match.group(1)}::{core_func_name}"

    # 遍历已执行的签名集合，看是否包含该核心函数名
    for exec_sig in executed_demangled_sigs:
        if core_func_name in exec_sig:
            return True

    return False


def is_function_exe(bugid, configPath):
    cfg = ConfigParser()
    cfg.read(configPath)

    baseFileListDir = cfg.get('llvm-locations', 'fileListDir')
    baseInfoDir = cfg.get('llvm-locations', 'infodir')
    fileListDir = os.path.join(baseFileListDir, bugid)
    method_info = os.path.join(baseInfoDir, bugid, 'failcov', 'method_info.txt')

    print(f"\n==============================================")
    print(f"🛡️ 开始基于动态覆盖率 (method_info.txt) 剔除未执行函数")
    print(f"==============================================\n")

    # 1. 获取所有被实际执行过的函数签名
    print("🔄 正在调用 c++filt 还原覆盖率文件中的修饰名...")
    executed_sigs = get_executed_demangled_functions(method_info)
    print(f"✅ 成功获取 {len(executed_sigs)} 个已被执行的底层函数签名。\n")

    if not executed_sigs:
        print("⚠️ 警告：覆盖率列表为空，将跳过过滤保护原数据。")
        return

    # 2. 遍历 LLM 生成的 *_functions.md 文件
    filtered_count = 0
    retained_count = 0

    for filename in os.listdir(fileListDir):
        if not filename.endswith('_functions.md'):
            continue

        file_path = os.path.join(fileListDir, filename)

        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # 正则匹配所有的 Markdown JSON 代码块
        json_blocks = re.findall(r'```json\n(.*?)\n```', content, re.DOTALL)

        retained_blocks = []

        for block in json_blocks:
            try:
                func_data = json.loads(block)
                llm_sig = func_data.get("signature", "")

                # 3. 核心比对：验证是否被执行过
                if is_signature_match(llm_sig, executed_sigs):
                    # 匹配成功，保留该 JSON 块
                    retained_blocks.append(f"```json\n{json.dumps(func_data, ensure_ascii=False, indent=2)}\n```")
                    retained_count += 1
                else:
                    filtered_count += 1
            except json.JSONDecodeError:
                print(f"⚠️ 跳过格式错误的 JSON 块")
                continue

        # 4. 将过滤后的函数列表重新写回文件（覆盖原文件，或另存为）
        filtered_file_path = os.path.join(fileListDir, filename.replace('.md', '_filtered.md'))
        with open(filtered_file_path, 'w', encoding='utf-8') as f:
            if retained_blocks:
                f.write("\n\n".join(retained_blocks))
            else:
                f.write("\n")

        print(f"📄 文件: {filename}")
        print(f"   - 保留了 {len(retained_blocks)} 个有效函数")
        print(f"   - 过滤了 {len(json_blocks) - len(retained_blocks)} 个死代码函数")

    print(f"\n🎉 覆盖率清洗完成！共保留 {retained_count} 个执行函数，剔除 {filtered_count} 个未执行函数。")


