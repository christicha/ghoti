import os
import re
import json
import shutil  # 用于彻底清空文件夹
import random  # 用于强制代投时随机挑选函数
from configparser import ConfigParser
from openai import OpenAI

from llvm.function_slice import function_slice, generate_function_summaries


def get_mutant_logs_content(mutants_log_dir, case_type, limit=10):
    """
    智能读取变异体日志：精准提取“运行结果”、“源码交织反汇编（案发现场）”与“差异对比”。
    剥离冗长的 AST 和原始 IR，确保 LLM 拥有最高质量的 Debug 上下文。
    :param case_type: "pass" 或 "fail"
    """
    content = ""
    count = 0

    if not os.path.exists(mutants_log_dir):
        return f"未找到变异体日志存储目录: {mutants_log_dir}\n"

    for root, dirs, files in os.walk(mutants_log_dir):
        if os.path.basename(root).startswith(case_type):
            for file in files:
                if file.endswith("_compile.log"):
                    log_path = os.path.join(root, file)
                    with open(log_path, 'r', encoding='utf-8') as f:
                        raw_text = f.read()

                        # 1. 提取运行结果 (掐头)
                        run_res = re.search(r'(\[1\. 运行结果\].*?)\n\n\[2\.', raw_text, re.DOTALL)
                        run_info = run_res.group(1) if run_res else "未提取到运行状态"

                        # 2. 提取源码交织反汇编 (留腹 - 找 Bug 最关键的案发现场！)
                        asm_res = re.search(r'(\[5\. 源码交织反汇编 \(重点对齐区\)\].*?)(?=\n={80,}|\n【🚨)', raw_text,
                                            re.DOTALL)
                        asm_info = asm_res.group(1) if asm_res else "未提取到交织反汇编"

                        # 3. 提取核心差异分析 (留尾)
                        diff_res = re.search(r'(【🚨 核心差异对比分析】.*)', raw_text, re.DOTALL)
                        diff_info = diff_res.group(1) if diff_res else "未提取到差异分析"

                        content += f"=== 变异体分析 ({case_type.upper()} 用例 {count + 1}): {file} ===\n"
                        content += f"{run_info}\n\n{asm_info}\n\n{diff_info}\n"
                        content += "-" * 80 + "\n\n"

                    count += 1
                    if count >= limit:
                        return content

    return content or f"暂无 {case_type} 类型的变异体日志。\n"


def empty_directory(folder_path):
    """清空指定文件夹下的所有内容，但保留文件夹本身"""
    if not os.path.exists(folder_path):
        return
    for filename in os.listdir(folder_path):
        file_path = os.path.join(folder_path, filename)
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.unlink(file_path)
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
        except Exception as e:
            print(f"⚠️ 清理失败 {file_path}. 原因: {e}")


def read_file_content(file_path):
    """安全读取文件内容的辅助函数"""
    if not os.path.exists(file_path):
        return f"⚠️ 找不到文件: {file_path}"
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        return f"⚠️ 读取文件失败 {file_path}: {str(e)}"


def extract_table_from_llm_response(response_text):
    """从 LLM 的 Markdown 响应中提取表格 (Rank, Function Name, Score)"""
    extracted_funcs = []
    lines = response_text.strip().split('\n')
    for line in lines:
        line = line.strip()
        if line.startswith('|') and '---' not in line:
            parts = [p.strip() for p in line.split('|')]
            # 确保列数足够且不是表头
            if len(parts) >= 4 and parts[1].lower() != 'rank' and '当前下钻轮次' not in parts[1]:
                try:
                    func_name = parts[2]
                    score = float(parts[3])
                    extracted_funcs.append({
                        "func_name": func_name,
                        "llm_score": score
                    })
                except ValueError:
                    pass
    return extracted_funcs


def normalize_sig(sig):
    """辅助函数：将签名中所有多余空格、不可见字符统统干掉，方便纯文本对比"""
    return re.sub(r'\s+', '', sig)


def get_ngrams(s, n=2):
    """将字符串切分为 n-gram 集合"""
    return set([s[i:i + n] for i in range(len(s) - n + 1)]) if len(s) >= n else set(s)


def jaccard_similarity(s1, s2):
    """计算两个字符串的杰卡德相似度系数"""
    set1, set2 = get_ngrams(s1), get_ngrams(s2)
    if not set1 or not set2:
        return 0.0
    return len(set1.intersection(set2)) / len(set1.union(set2))


def function_FL(bugid, configPath, rev, compilerOptionRight, compilerOptionWrong):
    cfg = ConfigParser()
    cfg.read(configPath)

    # 1. 获取路径配置
    baseLogDir = cfg.get('llvm-locations', 'logDir')
    baseResultDir = cfg.get('llvm-locations', 'resultDir')
    baseFileListDir = cfg.get('llvm-locations', 'fileListDir')
    baseCompilerDir = cfg.get('llvm-locations', 'compilersdir')
    basePassDir = cfg.get('llvm-locations', 'passdir')
    summaryDir = cfg.get('llvm-locations', 'summaryDir')
    api_key = cfg.get('deepseek', 'api')
    api_url = cfg.get('deepseek', 'url')

    # 定义各个文件的绝对路径
    bug_report_path = os.path.join('/home/chris/ghoti/benchmark/llvmbugs', bugid, 'version')
    log_path = os.path.join(baseLogDir, bugid, 'compile.log')
    sbfl_result = os.path.join(baseLogDir, bugid, 'SBFL.csv')
    summary_path = os.path.join(baseLogDir, bugid, 'abstract_summary.txt')
    compilerDir = os.path.join(baseCompilerDir, rev, rev, 'llvm')

    resultDir = os.path.join(baseResultDir, bugid)
    fileListDir_bugid = os.path.join(baseFileListDir, bugid)
    mutants_log_dir = os.path.join(baseLogDir, bugid, "mutants_logs")

    # ==========================================
    # ✅ 强制清空历史遗留的缓存文件
    # ==========================================
    print(f"\n🧹 正在清理 BugID: {bugid} 的历史工作区...")
    if os.path.exists(resultDir):
        empty_directory(resultDir)
    else:
        os.makedirs(resultDir, exist_ok=True)

    if os.path.exists(fileListDir_bugid):
        empty_directory(fileListDir_bugid)
    else:
        os.makedirs(fileListDir_bugid, exist_ok=True)
    print("✨ 清理完成，工作区已就绪！")

    # 2. 读取基础上下文数据
    bug_report = read_file_content(bug_report_path)
    deep_compile_content = read_file_content(log_path)

    print("📂 正在提取并压缩变异用例的编译分析日志...")
    passing_logs_content = get_mutant_logs_content(mutants_log_dir, "pass", limit=5)
    failing_logs_content = get_mutant_logs_content(mutants_log_dir, "fail", limit=5)

    print("🎯 正在基于 SBFL 排名截取 Top 100 嫌疑文件及摘要...")
    sbfl_result_raw = read_file_content(sbfl_result)
    abstract_content_raw = read_file_content(summary_path)

    top_100_sbfl_lines = []
    top_100_files = set()

    # 解析 SBFL.csv 获取 Top 100
    sbfl_lines = [line.strip() for line in sbfl_result_raw.strip().split('\n') if line.strip()]
    start_idx = 1 if sbfl_lines and "Rank" in sbfl_lines[0] else 0

    for line in sbfl_lines[start_idx:start_idx + 100]:
        parts = line.split(',')
        if len(parts) >= 3:
            rank, file_path, score = parts[0], parts[1], parts[2]
            top_100_sbfl_lines.append(f"{rank}. {file_path} (Score: {score})")
            top_100_files.add(file_path.strip())

    top_100_sbfl_str = "\n".join(top_100_sbfl_lines)

    # 联动解析摘要：只提取在 Top 100 列表中的文件摘要
    filtered_abstracts = []
    for line in abstract_content_raw.split('\n'):
        line = line.strip()
        if not line: continue

        file_part = line.split('，')[0].strip() if '，' in line else line.split(',')[0].strip()
        is_match = any(file_part.endswith(tf) or tf.endswith(file_part) for tf in top_100_files)
        if is_match:
            filtered_abstracts.append(line)

    top_100_abstract_str = "\n".join(filtered_abstracts)
    if not top_100_abstract_str:
        top_100_abstract_str = "⚠️ 未匹配到任何 Top 100 文件的功能摘要。"

    print(f"   ✅ SBFL 已截取前 {len(top_100_sbfl_lines)} 名，并成功匹配出 {len(filtered_abstracts)} 份核心文件摘要。")

    # 初始化 LLM 客户端
    client = OpenAI(api_key=api_key, base_url=api_url, timeout=360)

    # ==========================================
    # 🌟 阶段一：独立的根因分析 Agent (加入流式)
    # ==========================================
    print(f"\n🚀 [Agent 1] 开始执行全局缺陷根因推断...")
    rc_system_prompt = (
        "你是一个资深的 LLVM 编译器优化缺陷根因分析专家。你的任务是深度剖析编译日志、测试用例和变异体执行结果，"
        "推断出导致 Bug 的核心逻辑和潜在出错的编译器 Pass 阶段。"
    )

    rc_user_prompt = f"""
    请基于以下多源信息，深度分析导致该失败测试用例的编译器 Bug 根因：
    1. === Bug 报告 ===\n{bug_report}...
    2. === 原始缺陷代码在不同优化等级下的编译日志差异 ===\n{deep_compile_content}
    3. === 编译器源文件执行次数与功能摘要 (Top 100 嫌疑文件) ===\n{top_100_abstract_str}
    4. === SBFL 频谱定位结果 (Top 100 嫌疑榜单) === \n{top_100_sbfl_str}
    5. === 成功变异体的编译与指令突变日志 (参考) ===\n{passing_logs_content}
    6. === 失败变异体的编译与指令突变日志 (重点分析) ===\n{failing_logs_content}

    请给出一份逻辑严密的“全局缺陷根因分析报告”，不需要具体指出文件，只需指出可能出问题的优化阶段、数据结构或算法逻辑。
    """

    rc_messages = [
        {"role": "system", "content": rc_system_prompt},
        {"role": "user", "content": rc_user_prompt}
    ]

    root_cause_report = ""
    root_cause_reasoning = ""
    try:
        print("  └─ 🧠 正在思考根因: \n", end="", flush=True)
        rc_response = client.chat.completions.create(model="deepseek-reasoner", messages=rc_messages, stream=True)

        has_printed_content = False
        for chunk in rc_response:
            delta = chunk.choices[0].delta
            # 捕获思考过程
            r_text = getattr(delta, 'reasoning_content', None)
            if r_text:
                root_cause_reasoning += r_text
                print(r_text, end="", flush=True)

            # 捕获最终输出
            c_text = getattr(delta, 'content', None)
            if c_text:
                if not has_printed_content:
                    print("\n\n  └─ 📝 最终报告: \n", end="", flush=True)
                    has_printed_content = True
                root_cause_report += c_text
                print(c_text, end="", flush=True)

        print("\n✅ 全局根因分析报告生成完毕。")
        # 兜底：如果最终报告为空，取思考过程
        root_cause_report = root_cause_report if root_cause_report.strip() else root_cause_reasoning

        # 💾 保存根因分析报告到 result 文件夹
        rc_report_path = os.path.join(resultDir, 'root_cause_report.md')
        with open(rc_report_path, 'w', encoding='utf-8') as f:
            f.write(f"# BugID: {bugid} - 全局缺陷根因分析报告\n\n")
            f.write(root_cause_report)
        print(f"💾 根因分析报告已保存至: {rc_report_path}")

    except Exception as e:
        print(f"\n❌ 根因分析异常: {str(e)}")
        root_cause_report = "⚠️ 根因分析生成失败，请仅基于 SBFL 榜单进行推断。"

    # ==========================================
    # 🌟 阶段二：缺陷定位 Agent - 一次性抽取 10 个可疑文件 (加入流式)
    # ==========================================
    print(f"\n🚀 [Agent 2] 基于根因报告，一次性抽取 Top 10 可疑文件...")
    loc_system_prompt = (
        "你是一个资深的 LLVM 编译器优化缺陷定位专家。你需要基于根因分析，"
        "从给定的嫌疑文件列表中，精准锁定最可疑的源文件集合。"
    )

    file_selection_prompt = f"""
    【全局缺陷根因分析报告】
    {root_cause_report}

    【备选文件池】
    === 编译器源文件执行次数与功能摘要 (Top 100 嫌疑文件) ===\n{top_100_abstract_str}
    === SBFL 频谱定位结果 (Top 100 嫌疑榜单) === \n{top_100_sbfl_str}

    【你的任务】
    请结合上述根因分析报告，从备选文件池中挑选出最有可能包含缺陷的10个源文件，并按照嫌疑程度从高到低排序（第 1 个最可疑）。

    【输出规范】
    请在回复的最末尾，严格以如下格式输出这 10 个文件路径（必须带有后缀如 .cpp, .h），每行一个。绝对不要在文件路径前后加任何多余字符或编号。
    === FILES START ===
    [路径1]
    [路径2]
    ...
    [路径10]
    === FILES END ===
    """

    loc_messages = [
        {"role": "system", "content": loc_system_prompt},
        {"role": "user", "content": file_selection_prompt}
    ]

    extracted_10_files = []
    file_response_text = ""
    file_reasoning_text = ""
    try:
        print("  └─ 🧠 正在筛选文件: \n", end="", flush=True)
        file_res = client.chat.completions.create(model="deepseek-reasoner", messages=loc_messages, stream=True)

        has_printed_content = False
        for chunk in file_res:
            delta = chunk.choices[0].delta
            r_text = getattr(delta, 'reasoning_content', None)
            if r_text:
                file_reasoning_text += r_text
                # print(r_text, end="", flush=True)

            c_text = getattr(delta, 'content', None)
            if c_text:
                if not has_printed_content:
                    print("\n\n  └─ 📝 提取结果: \n", end="", flush=True)
                    has_printed_content = True
                file_response_text += c_text
                # print(c_text, end="", flush=True)

        print("\n")
        file_response_text = file_response_text if file_response_text.strip() else file_reasoning_text

        # 解析抽取的 10 个文件
        match = re.search(r'===\s*FILES START\s*=== \n(.*?)\n===\s*FILES END\s*===', file_response_text,
                          re.DOTALL | re.IGNORECASE)
        if match:
            raw_files = match.group(1).split('\n')
            extracted_10_files = [f.strip() for f in raw_files if f.strip() and ('.c' in f or '.h' in f)]
        else:
            # 兜底解析：用正则匹配最后出现的代码文件路径
            paths = re.findall(r'([a-zA-Z0-9_/\-\.]+\.(?:cpp|c|h|hpp))', file_response_text)
            extracted_10_files = list(dict.fromkeys(paths))[-10:]  # 去重取最后10个
    except Exception as e:
        print(f"\n❌ 批量抽取文件异常: {str(e)}")
        # 兜底：直接取 SBFL 前 10
        extracted_10_files = list(top_100_files)[:10]

    if not extracted_10_files:
        print("❌ 未能成功提取可疑文件列表，程序终止。")
        return

    print(f"🎯 成功锁定 Top 10 可疑文件:\n" + "\n".join([f"  {i + 1}. {f}" for i, f in enumerate(extracted_10_files)]))

    all_functions_data = []

    # ==========================================
    # 🌟 阶段三：循环遍历这 10 个文件进行独立下钻
    # ==========================================
    for file_rank, suspect_file_path in enumerate(extracted_10_files, 1):
        print(f"\n==========================================")
        print(f"🔄 当前排查进度：文件 {file_rank} / 10 -> `{suspect_file_path}`")
        print(f"==========================================")

        # --- 路径容错匹配机制 ---
        full_suspect_path_check = os.path.join(compilerDir, suspect_file_path)
        if suspect_file_path not in top_100_files and not os.path.exists(full_suspect_path_check):
            best_match = None
            best_sim = 0.0
            for tf in top_100_files:
                if tf.endswith(suspect_file_path) or suspect_file_path.endswith(tf):
                    best_match, best_sim = tf, 1.0
                    break
                sim = jaccard_similarity(suspect_file_path, tf)
                if sim > best_sim:
                    best_sim, best_match = sim, tf

            if best_match and best_sim > 0.3:
                print(f"⚠️ 自动重定向文件路径: `{suspect_file_path}` -> `{best_match}` (Sim: {best_sim:.2f})")
                suspect_file_path = best_match
            else:
                print(f"❌ 路径 `{suspect_file_path}` 不存在，跳过。")
                continue

        # --- AST 切片 ---
        full_suspect_path = os.path.join(compilerDir, suspect_file_path)
        print("🔪 正在执行 AST 切片与摘要提纯...")
        function_slice(bugid, configPath, full_suspect_path)
        generate_function_summaries(bugid, configPath, full_suspect_path)

        filename_only = os.path.basename(suspect_file_path)
        no_body_path = os.path.join(fileListDir_bugid, f"{filename_only}_no_body.md")
        with_body_path = os.path.join(fileListDir_bugid, f"{filename_only}_with_body.md")
        json_summary_path = os.path.join(summaryDir, f"{filename_only}.json")

        file_summary = "⚠️ 暂无全局功能摘要"
        if os.path.exists(json_summary_path):
            try:
                with open(json_summary_path, 'r', encoding='utf-8') as jf:
                    file_summary = json.load(jf).get("summary", file_summary)
            except:
                pass

        functions_no_body_content = read_file_content(no_body_path)
        functions_with_body_content = read_file_content(with_body_path)

        if not functions_no_body_content or "⚠️" in functions_no_body_content:
            print(f"⏭️ 文件 {filename_only} 无有效切片，跳过。")
            continue

        sig_to_code = {}
        matches = re.finditer(r'### 📍 函数签名: `(.*?)`\n\n```cpp\n(.*?)\n```', functions_with_body_content, re.DOTALL)
        for m in matches:
            sig_to_code[m.group(1).strip()] = m.group(2).strip()

        # 🎯 为当前文件初始化局部交互上下文
        drill_system_prompt = (
            "你是一个资深的 LLVM 编译器缺陷下钻专家。我们将为一个可疑文件进行最多 30 轮的函数代码审查。\n"
            "你会基于已知的根因分析上下文，挑选函数索要源码，并进行 0-10 分的打分。\n"
            "请严格按照表格格式输出打分，并仅输出下一步要求的函数签名。"
        )
        drill_messages = [{"role": "system", "content": drill_system_prompt}]

        # 传递根因上下文
        context_prompt = f"""
        【全局根因分析指导】
        {root_cause_report}

        当前正在深度排查的第 {file_rank} 可疑文件是：`{suspect_file_path}`
        该文件的核心功能摘要为：`{file_summary}`
        """
        drill_messages.append({"role": "user", "content": context_prompt})
        drill_messages.append({"role": "assistant",
                               "content": "收到，我已经理解了 Bug 的根因逻辑及当前文件的基本功能。请提供该文件的函数概览，我将开始挑选可疑函数。"})

        round_funcs_for_this_file = []
        requested_sig = ""
        explored_sigs = set()
        total_funcs_in_file = len(sig_to_code)
        MIN_EXPLORE_ROUNDS = min(30, total_funcs_in_file)
        MAX_FUNC_ROUNDS = 30

        print(f"\n🔍 开启文件 {filename_only} 的内部函数下钻排查 (最少 {MIN_EXPLORE_ROUNDS} 轮)...")

        # --- 函数 30 轮循环下钻 ---
        for func_round_idx in range(1, MAX_FUNC_ROUNDS + 1):
            if func_round_idx == 1:
                prompt_function_overview = f"""
                【当前阶段：函数概览与深度下钻 (1/{MAX_FUNC_ROUNDS})】
                以下是该文件在处理失败测试用例时实际执行过的函数概览（仅包含函数签名和功能摘要）：
                === 函数概览 ===
                {functions_no_body_content}
                ===============

                【你的任务】
                请基于你掌握的全局根因，挑选出最可疑的 1 个函数，并要求查看其完整源码。
                【输出规范】
                简要说明理由。然后在最后一行，仅且只输出想要查看的完整函数签名（不带反引号）。
                ⚠️ 注意：这是第一轮，绝对不能提前结束分析。
                """
                drill_messages.append({"role": "user", "content": prompt_function_overview})

                print(f"  └─ 🕵️‍♂️ [R-1] 正在请求挑选首个嫌疑函数...\n  思考过程: \n", end="", flush=True)
                try:
                    res = client.chat.completions.create(model="deepseek-reasoner", messages=drill_messages,
                                                         stream=True)
                    reply_text = ""
                    reasoning_text = ""
                    has_printed_content = False

                    for chunk in res:
                        delta = chunk.choices[0].delta
                        r_text = getattr(delta, 'reasoning_content', None)
                        if r_text:
                            reasoning_text += r_text
                            # print(r_text, end="", flush=True)

                        c_text = getattr(delta, 'content', None)
                        if c_text:
                            if not has_printed_content:
                                print("\n\n  最终输出: \n", end="", flush=True)
                                has_printed_content = True
                            reply_text += c_text
                            # print(c_text, end="", flush=True)

                    print("\n")
                    reply_text = reply_text if reply_text.strip() else reasoning_text

                    drill_messages.append({"role": "assistant", "content": reply_text})
                    lines = [l.strip() for l in reply_text.split('\n') if l.strip()]
                    requested_sig = lines[-1].replace('`', '')
                except Exception as e:
                    print(f"\n  ❌ 索要签名异常: {e}")
                    break

            else:
                # 🛑 防逃逸拦截
                if "[END_FILE_ANALYSIS]" in requested_sig:
                    if func_round_idx <= MIN_EXPLORE_ROUNDS:
                        print(f"  ⚠️ 拦截：大模型试图提前退出！(保底要求 {MIN_EXPLORE_ROUNDS} 轮)")
                        unexplored = [s for s in sig_to_code.keys() if normalize_sig(s) not in explored_sigs]
                        if unexplored:
                            requested_sig = unexplored[0]
                            print(f"  └─ 🤖 脚本强行代投下一个未排查函数...")
                        else:
                            break
                    else:
                        print(f"  ✅ 模型判定该文件排查完毕，正常结束下钻。")
                        break

                explored_sigs.add(normalize_sig(requested_sig))
                requested_function_source_code = sig_to_code.get(requested_sig)

                # 模糊匹配兜底
                if not requested_function_source_code:
                    norm_req_sig = normalize_sig(requested_sig)
                    for s, c in sig_to_code.items():
                        norm_s = normalize_sig(s)
                        if requested_sig in s or s in requested_sig or norm_req_sig in norm_s or norm_s in norm_req_sig:
                            requested_function_source_code, requested_sig = c, s
                            break

                if not requested_function_source_code:
                    requested_function_source_code = f"⚠️ 严重提示：未找到匹配 `{requested_sig}` 的源码。请重新检查概览，如果无可疑项，请输出 [END_FILE_ANALYSIS]。"

                explored_list_str = "\n".join([f"- {s}" for s in list(explored_sigs)]) if explored_sigs else "无"

                exit_guidance = (
                    f"⚠️ 强制要求：必须排查至少 {MIN_EXPLORE_ROUNDS} 个函数，禁止结束。"
                    if func_round_idx < MIN_EXPLORE_ROUNDS else
                    "如果没有其他可疑函数，请在最后一行输出：`[END_FILE_ANALYSIS]`。"
                )

                prompt_function_drill_down = f"""
                【当前阶段：源码分析与下钻 ({func_round_idx}/{MAX_FUNC_ROUNDS})】
                请求的函数完整源码如下：
                === 函数实现 ===
                {requested_function_source_code}
                ===============

                已排查过的历史函数：\n{explored_list_str}

                【你的任务】
                1. 为该函数打分 (0-10分，基于之前的根因分析)。
                2. 从概览中挑选下一个没看过的函数。

                【输出规范】
                | Rank (当前下钻轮次) | Function Name | Score |
                |---|---|---|
                | {func_round_idx} | [当前查看的函数名] | [你的打分] |

                {exit_guidance}
                最后一行仅且只输出下一步想要查看的函数签名或结束标志，不要带额外字符。
                """
                drill_messages.append({"role": "user", "content": prompt_function_drill_down})

                print(
                    f"  └─ 🔬 [{func_round_idx}/{MAX_FUNC_ROUNDS}] 审查源码 `{requested_sig}` 并打分...\n  思考过程: \n",
                    end="", flush=True)
                try:
                    res = client.chat.completions.create(model="deepseek-reasoner", messages=drill_messages,
                                                         stream=True)
                    reply_text = ""
                    reasoning_text = ""
                    has_printed_content = False

                    for chunk in res:
                        delta = chunk.choices[0].delta
                        r_text = getattr(delta, 'reasoning_content', None)
                        if r_text:
                            reasoning_text += r_text
                            # print(r_text, end="", flush=True)

                        c_text = getattr(delta, 'content', None)
                        if c_text:
                            if not has_printed_content:
                                print("\n\n  最终输出: \n", end="", flush=True)
                                has_printed_content = True
                            reply_text += c_text
                            # print(c_text, end="", flush=True)

                    print("\n")
                    reply_text = reply_text if reply_text.strip() else reasoning_text

                    drill_messages.append({"role": "assistant", "content": reply_text})

                    extracted_tables = extract_table_from_llm_response(reply_text)
                    if extracted_tables:
                        round_funcs_for_this_file.extend(extracted_tables)
                        print(f"    🌟 该函数打分: {extracted_tables[0]['llm_score']}")

                    lines = [l.strip() for l in reply_text.split('\n') if l.strip()]
                    requested_sig = lines[-1].replace('`', '')
                except Exception as e:
                    print(f"\n  ❌ 源码审查异常: {e}")
                    break

        # --- 单文件下钻结束，保存并聚合数据 ---
        if round_funcs_for_this_file:
            single_round_md_path = os.path.join(resultDir, f"rank_{file_rank}_{filename_only}_funcs.md")
            with open(single_round_md_path, 'w', encoding='utf-8') as srf:
                srf.write(f"# Top {file_rank} 嫌疑文件函数排名 - `{filename_only}`\n\n")
                srf.write("| 探测顺序 | Function Name | Score |\n|---|---|---|\n")
                for idx, func in enumerate(round_funcs_for_this_file, 1):
                    srf.write(f"| {idx} | `{func['func_name']}` | {func['llm_score']} |\n")

            for func in round_funcs_for_this_file:
                func['file_rank'] = file_rank
                func['file_name'] = suspect_file_path
                all_functions_data.append(func)

            print(f"💾 Top {file_rank} 嫌疑文件排查完毕，排名独立保存: {single_round_md_path}")
        else:
            print(f"⚠️ Top {file_rank} 文件未提取到有效打分。")

    # ==========================================
    # 🏁 终极汇总与重排版 (双重榜单对比输出)
    # ==========================================
    if not all_functions_data:
        print("\n❌ 严重警告：全局排查未能提取到任何有效的函数打分。")
        return

    print("\n📈 正在进行全局综合分数计算及双榜单生成...")
    gamma = 0.85
    for item in all_functions_data:
        # file_rank 越小越排前，衰减计算不变
        item['final_score'] = round(item['llm_score'] * (gamma ** (item['file_rank'] - 1)), 4)

    # ------------------------------------------
    # 榜单 1：基于指数衰减策略排序 (综合考虑了文件级优先级)
    # ------------------------------------------
    decay_sorted_data = sorted(all_functions_data, key=lambda x: x['final_score'], reverse=True)

    decay_ranked_md_path = os.path.join(resultDir, 'ultimate_function_rank_decay.md')
    decay_md_content = f"# 🎯 BugID: {bugid} - 全局函数缺陷定位 (指数衰减策略)\n\n"
    decay_md_content += f"> **参数说明**: 基础衰减因子 Gamma = {gamma} \n"
    decay_md_content += f"> **计算公式**: Final_Score = LLM_Score * (Gamma ^ (File_Rank - 1))\n\n"
    decay_md_content += "| 全局排名 | 综合得分 | 所在文件 (Rank) | 函数名称 | 原始 LLM 评分 |\n"
    decay_md_content += "|:---:|:---:|---|---|:---:|\n"

    for idx, item in enumerate(decay_sorted_data, 1):
        decay_md_content += f"| {idx} | **{item['final_score']}** | {os.path.basename(item['file_name'])} (R{item['file_rank']}) | `{item['func_name']}` | {item['llm_score']} |\n"

    with open(decay_ranked_md_path, 'w', encoding='utf-8') as f:
        f.write(decay_md_content)

    # ------------------------------------------
    # 榜单 2：仅基于纯函数可疑度排序 (剥离文件影响)
    # ------------------------------------------
    # 二级排序规则：首选 llm_score 降序；若分数相同，则按照 file_rank 升序 (来自越高排名文件的排越前)
    raw_sorted_data = sorted(all_functions_data, key=lambda x: (x['llm_score'], -x['file_rank']), reverse=True)

    raw_ranked_md_path = os.path.join(resultDir, 'ultimate_function_rank_raw.md')
    raw_md_content = f"# 🎯 BugID: {bugid} - 全局函数缺陷定位 (纯函数可疑度排行)\n\n"
    raw_md_content += f"> **说明**: 仅根据 LLM 对函数的直接打分 (0-10) 进行排序。当函数分数相同时，原文件排名更靠前的函数优先。\n\n"
    raw_md_content += "| 全局排名 | 原始 LLM 评分 | 所在文件 (Rank) | 函数名称 | 综合得分 (参考) |\n"
    raw_md_content += "|:---:|:---:|---|---|:---:|\n"

    for idx, item in enumerate(raw_sorted_data, 1):
        raw_md_content += f"| {idx} | **{item['llm_score']}** | {os.path.basename(item['file_name'])} (R{item['file_rank']}) | `{item['func_name']}` | {item['final_score']} |\n"

    with open(raw_ranked_md_path, 'w', encoding='utf-8') as f:
        f.write(raw_md_content)

    print(f"🎉 全部分析排查完成！已生成双重对比榜单：")
    print(f"  📂 1. 指数衰减排行: {decay_ranked_md_path}")
    print(f"  📂 2. 纯函数打分排行: {raw_ranked_md_path}")