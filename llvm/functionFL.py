import json
import os
import re
import subprocess
from configparser import ConfigParser
from openai import OpenAI
from llvm.fileTop10 import read_file_content


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
        为了便于自动化脚本解析，对于你提取出的每一个函数，请单独使用一个 Markdown JSON 代码框进行输出(即```json)。
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


def extract_log_diff(log_path):
    """辅助函数：从完整的 compile.log 中提取出底部的差异对比分析部分"""
    if not os.path.exists(log_path):
        return "⚠️ 编译日志不存在。"

    with open(log_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # 尝试匹配我们之前在日志中定义的差异对比分割线
    # 兼容两种可能的写法："【🚨 核心差异对比分析】" 或 "【跨配置核心差异对比】"
    match = re.search(r'【(?:🚨 核心|跨配置核心)差异对比.*?】(.*)', content, re.DOTALL)
    if match:
        return match.group(0).strip()

    # 如果没找到特定的标题，提取最后 2000 个字符作为 fallback
    return "⚠️ 未找到标准的差异对比分割线，截取日志尾部特征：\n" + content[-2000:]


def function_FL(bugid, configPath):
    cfg = ConfigParser()
    cfg.read(configPath)

    # 1. 获取路径配置
    baseLogDir = cfg.get('llvm-locations', 'logDir')
    baseResultDir = cfg.get('llvm-locations', 'resultDir')
    baseFileListDir = cfg.get('llvm-locations', 'fileListDir')

    logDir = os.path.join(baseLogDir, bugid)
    resultDir = os.path.join(baseResultDir, bugid)
    fileListDir = os.path.join(baseFileListDir, bugid)

    # 获取 LLM API 配置
    api_key = cfg.get('deepseek', 'api')
    api_url = cfg.get('deepseek', 'url')

    print(f"\n==============================================")
    print(f"🎯 开始为 BugID: {bugid} 执行最终函数级缺陷定位")
    print(f"==============================================\n")

    # 2. 收集线索数据
    # 线索 1：根因分析报告
    rca_path = os.path.join(logDir, 'root_cause.md')
    rca_content = read_file_content(rca_path)
    if not rca_content:
        print("❌ 致命错误：找不到根因分析报告 (root_cause.md)。")
        return

    # 线索 2：编译日志核心差异
    compile_log_path = os.path.join(logDir, 'compile.log')
    log_diff_content = extract_log_diff(compile_log_path)

    # 线索 3：高危文件级排名 (提取 CSV 的前 10 行文本表现形式)
    rank_csv_path = os.path.join(resultDir, 'final_file_rank.csv')
    file_rank_str = ""
    if os.path.exists(rank_csv_path):
        try:
            with open(rank_csv_path, 'r', encoding='utf-8') as f:
                # 简单读取前 11 行（包含表头）作为文本
                lines = f.readlines()
                file_rank_str = "".join(lines[:11])
        except Exception as e:
            file_rank_str = f"⚠️ 读取文件排名失败: {str(e)}"
    else:
        file_rank_str = "⚠️ final_file_rank.csv 不存在。"

    # 线索 4：动态覆盖率验证后的候选函数库 (*_filtered.md)
    candidate_functions = []
    if os.path.exists(fileListDir):
        for filename in os.listdir(fileListDir):
            if filename.endswith('_filtered.md'):
                # 提取原文件名，比如 LoopVectorize.cpp_functions_filtered.md -> LoopVectorize.cpp
                orig_filename = filename.replace('_functions_filtered.md', '')
                content = read_file_content(os.path.join(fileListDir, filename))
                if content.strip() and "未被覆盖率命中" not in content:
                    candidate_functions.append(f"### 文件: {orig_filename}\n{content}\n")

    candidate_functions_str = "\n".join(candidate_functions)
    if not candidate_functions_str.strip():
        print("❌ 致命错误：候选函数库为空！(可能所有函数都被覆盖率清洗掉了)")
        return

    # 3. 组装 Prompt
    system_prompt = (
        "你是一位世界顶级的 LLVM 编译器底层研发专家与高级静态分析师。"
        "你精通编译器优化管线（Pass Pipeline）、指令选择（Instruction Selection）、"
        "IR 转换逻辑以及 x86-64 汇编差异分析。"
    )

    user_prompt = f"""# 任务目标
    我们正在进行编译器缺陷定位的最终阶段：函数级定位。
    我已经通过多维度的交叉验证，为你锁定了极小范围的嫌疑函数集合（这些函数均已被证实会在触发 Bug 的测试用例中执行）。
    请你综合我提供的“根因分析报告”、“编译日志核心差异”以及“高危文件排名”，对提供的“候选函数库”进行深度审查，最终排查并给出一份包含所有函数的可疑度最终排名。
    
    输入数据
    1. 缺陷根因分析报告
    【说明：这是我们前期推导出的编译器逻辑断层与嫌疑 Pass 画像。】
    {rca_content}
    2. 编译日志核心差异
    【说明：这是触发 Bug 的优化级别与正常优化级别在底层汇编和 Pass 执行上的直接物理证据。】
    {log_diff_content}
    3. 高危文件级排名
    【说明：候选函数所在文件的宏观可疑度背景，排名越靠前，其内部函数藏匿 Bug 的概率越高。】
    {file_rank_str}
    4. 动态覆盖率验证后的候选函数库
    【说明：这是从高危文件中提取出的、且被真实执行过的核心业务函数及其功能摘要。】
    {candidate_functions_str}
    
    分析指令（请严格按以下思维链执行）
    第一步：锁定罪证特征
    分析【编译日志核心差异】，明确到底是哪条具体的机器指令被错误生成或错误删除了。结合【RCA 报告】，用一句话明确我们要在候选函数中寻找什么样的“作案动机”。
    注：不仅要关注直接生成/折叠该指令的表面逻辑，还要思考支撑该逻辑的底层分析依赖。
    第二步：函数职责“审问”与调用链穿透
    逐一阅读【候选函数库】中的函数签名与功能摘要：
    1. 直接职责匹配：该函数的作用范围是否恰好覆盖了第一步锁定的“罪证特征”？
    2. 职责下放与嵌套审查 (Caller-Callee Consideration) [核心要点]：绝对不要只盯着表面的“入口函数”（Dispatcher）！ 如果函数 A（看似极度可疑的顶层处理函数）的功能实现高度依赖内部调用的辅助函数 B，你必须同时评估 A 和 B。仔细甄别谁才是真正的“幕后黑手”。
    3. 上下文权重：结合该函数所在的【高危文件排名】，如果它处于 Top 1 或 Top 2 的文件中，其嫌疑度应被基础放大。
    
    输出格式要求
    请提供一个 Markdown 表格，按照可疑指数排名，要求表格包含所有文件中所有给定的候选函数，功能类似的函数可以并列排名
    | Rank | 所属文件 | 函数签名 (Signature) | 可疑指数 (1-10) |
    """

    print("🧠 数据装载完毕，正在呼叫大模型执行函数排名 (请耐心等待)...")
    try:
        client = OpenAI(api_key=api_key, base_url=api_url, timeout=3600)
        response = client.chat.completions.create(
            model="deepseek-reasoner",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3,
            max_tokens=50000
        )
        final_report = response.choices[0].message.content

        # 4. 保存最终报告
        out_file_path = os.path.join(resultDir, 'function_level_localization.md')
        with open(out_file_path, 'w', encoding='utf-8') as out_f:
            out_f.write(final_report)

        print(f"✅ 成功！函数级缺陷定位报告已保存至: {out_file_path}")

    except Exception as e:
        print(f"❌ API 调用失败: {str(e)}")



