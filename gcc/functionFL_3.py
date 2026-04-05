import os
import re
import json
import shutil  # 用于彻底清空文件夹
import random  # 用于强制代投时随机挑选函数
from configparser import ConfigParser
from openai import OpenAI

# ==========================================
# ⚠️ 注意：这里修改为导入之前为你生成的 gcc 版本的模块
# ==========================================
from gcc.function_slice import function_slice, generate_function_summaries


def get_mutant_logs_content(mutants_log_dir, case_type, limit=10):
    """
    智能读取变异体日志：针对 GCC 版本的日志格式进行提取。
    精准提取“运行结果”、“源码交织反汇编（案发现场）”与“差异对比”。
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

                        # 2. 提取源码交织反汇编 (留腹 - GCC 指令分析关键点)
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
    """辅助函数：清理签名方便对比"""
    return re.sub(r'\s+', '', sig)


def get_ngrams(s, n=2):
    return set([s[i:i + n] for i in range(len(s) - n + 1)]) if len(s) >= n else set(s)


def jaccard_similarity(s1, s2):
    set1, set2 = get_ngrams(s1), get_ngrams(s2)
    if not set1 or not set2:
        return 0.0
    return len(set1.intersection(set2)) / len(set1.union(set2))


def function_FL(bugid, configPath, rev, compilerOptionRight, compilerOptionWrong):
    cfg = ConfigParser()
    cfg.read(configPath)

    # 1. 获取路径配置 (全部切换为 gcc-locations)
    baseLogDir = cfg.get('gcc-locations', 'logDir')
    baseResultDir = cfg.get('gcc-locations', 'resultDir')
    baseFileListDir = cfg.get('gcc-locations', 'fileListDir')
    baseCompilerDir = cfg.get('gcc-locations', 'compilersdir')
    basePassDir = cfg.get('gcc-locations', 'passdir')
    summaryDir = cfg.get('gcc-locations', 'summaryDir')
    api_key = cfg.get('deepseek', 'api')
    api_url = cfg.get('deepseek', 'url')

    # 定义各个文件的绝对路径 (Benchmark 路径切换)
    bug_report_path = os.path.join('/home/chris/ghoti/benchmark/gccbugs', bugid, 'version')
    log_path = os.path.join(baseLogDir, bugid, 'compile.log')
    sbfl_result = os.path.join(baseLogDir, bugid, 'SBFL.csv')
    summary_path = os.path.join(baseLogDir, bugid, 'abstract_summary.txt')
    compilerDir = os.path.join(baseCompilerDir, rev, rev)

    resultDir = os.path.join(baseResultDir, bugid)
    fileListDir_bugid = os.path.join(baseFileListDir, bugid)
    mutants_log_dir = os.path.join(baseLogDir, bugid, "mutants_logs")

    # ==========================================
    # ✅ 清理历史缓存
    # ==========================================
    print(f"\n🧹 正在清理 GCC BugID: {bugid} 的历史工作区...")
    if os.path.exists(resultDir):
        empty_directory(resultDir)
    else:
        os.makedirs(resultDir, exist_ok=True)

    if os.path.exists(fileListDir_bugid):
        empty_directory(fileListDir_bugid)
    else:
        os.makedirs(fileListDir_bugid, exist_ok=True)

    # 2. 读取上下文
    bug_report = read_file_content(bug_report_path)
    deep_compile_content = read_file_content(log_path)

    print("📂 正在提取并压缩 GCC 变异用例的编译分析日志...")
    passing_logs_content = get_mutant_logs_content(mutants_log_dir, "pass", limit=5)
    failing_logs_content = get_mutant_logs_content(mutants_log_dir, "fail", limit=5)

    print("🎯 正在基于 SBFL 排名截取 Top 100 嫌疑文件及摘要...")
    sbfl_result_raw = read_file_content(sbfl_result)
    abstract_content_raw = read_file_content(summary_path)

    top_100_sbfl_lines = []
    top_100_files = set()

    sbfl_lines = [line.strip() for line in sbfl_result_raw.strip().split('\n') if line.strip()]
    start_idx = 1 if sbfl_lines and "Rank" in sbfl_lines[0] else 0

    for line in sbfl_lines[start_idx:start_idx + 100]:
        parts = line.split(',')
        if len(parts) >= 3:
            rank, file_path, score = parts[0], parts[1], parts[2]
            top_100_sbfl_lines.append(f"{rank}. {file_path} (Score: {score})")
            top_100_files.add(file_path.strip())

    top_100_sbfl_str = "\n".join(top_100_sbfl_lines)

    # 解析摘要
    filtered_abstracts = []
    for line in abstract_content_raw.split('\n'):
        line = line.strip()
        if not line: continue
        file_part = line.split('，')[0].strip() if '，' in line else line.split(',')[0].strip()
        is_match = any(file_part.endswith(tf) or tf.endswith(file_part) for tf in top_100_files)
        if is_match:
            filtered_abstracts.append(line)

    top_100_abstract_str = "\n".join(filtered_abstracts)

    client = OpenAI(api_key=api_key, base_url=api_url, timeout=360)

    # ==========================================
    # 🌟 阶段一：GCC 根因分析 Agent
    # ==========================================
    print(f"\n🚀 [Agent 1] 开始执行 GCC 全局缺陷根因推断...")
    rc_system_prompt = (
        "你是一个资深的 GCC 编译器优化缺陷根因分析专家。你的任务是深度剖析编译日志、测试用例和变异体执行结果，"
        "推断出导致 Bug 的核心逻辑和潜在出错的编译器 Pass 阶段（如 Tree SSA, RTL 优化等）。"
    )

    rc_user_prompt = f"""
    请基于以下多源信息，深度分析导致该失败测试用例的 GCC 编译器 Bug 根因：
    1. === Bug 报告 ===\n{bug_report}...
    2. === 原始缺陷代码在不同优化等级下的编译日志差异 ===\n{deep_compile_content}
    3. === 编译器源文件执行次数与功能摘要 (Top 100 嫌疑文件) ===\n{top_100_abstract_str}
    4. === SBFL 频谱定位结果 (Top 100 嫌疑榜单) === \n{top_100_sbfl_str}
    5. === 成功变异体的编译与指令突变日志 (参考) ===\n{passing_logs_content}
    6. === 失败变异体的编译与指令突变日志 (重点分析) ===\n{failing_logs_content}

    请给出一份逻辑严密的“全局缺陷根因分析报告”，指出可能出问题的 GCC 优化阶段（如 GIMPLE 变换, Loop 优化等）、数据结构或算法逻辑。
    """
    rc_messages = [{"role": "system", "content": rc_system_prompt}, {"role": "user", "content": rc_user_prompt}]

    root_cause_report = ""
    root_cause_reasoning = ""
    try:
        print("  └─ 🧠 正在思考根因: \n", end="", flush=True)
        rc_response = client.chat.completions.create(model="deepseek-reasoner", messages=rc_messages, stream=True)

        has_printed_content = False
        for chunk in rc_response:
            delta = chunk.choices[0].delta
            r_text = getattr(delta, 'reasoning_content', None)
            if r_text:
                root_cause_reasoning += r_text
                print(r_text, end="", flush=True)
            c_text = getattr(delta, 'content', None)
            if c_text:
                if not has_printed_content:
                    print("\n\n  └─ 📝 最终报告: \n", end="", flush=True)
                    has_printed_content = True
                root_cause_report += c_text
                print(c_text, end="", flush=True)

        print("\n✅ GCC 全局根因分析报告生成完毕。")
        root_cause_report = root_cause_report if root_cause_report.strip() else root_cause_reasoning

        rc_report_path = os.path.join(resultDir, 'root_cause_report.md')
        with open(rc_report_path, 'w', encoding='utf-8') as f:
            f.write(f"# BugID: {bugid} - GCC 全局缺陷根因分析报告\n\n")
            f.write(root_cause_report)
        print(f"💾 根因分析报告已保存至: {rc_report_path}")

    except Exception as e:
        print(f"\n❌ 根因分析异常: {str(e)}")
        root_cause_report = "⚠️ 根因分析生成失败，请仅基于 SBFL 榜单进行推断。"

    # ==========================================
    # 🌟 阶段二：缺陷定位 Agent - 抽取 GCC 嫌疑文件
    # ==========================================
    print(f"\n🚀 [Agent 2] 基于根因报告，筛选 GCC Top 10 可疑文件...")
    loc_system_prompt = (
        "你是一个资深的 GCC 编译器优化缺陷定位专家。你需要基于根因分析，"
        "从嫌疑文件列表中，精准锁定最可疑的 GCC 源代码集合。"
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

    loc_messages = [{"role": "system", "content": loc_system_prompt},
                    {"role": "user", "content": file_selection_prompt}]

    extracted_10_files = []
    file_response_text = ""
    try:
        file_res = client.chat.completions.create(model="deepseek-reasoner", messages=loc_messages, stream=True)
        for chunk in file_res:
            c_text = getattr(chunk.choices[0].delta, 'content', None)
            if c_text: file_response_text += c_text

        # 支持 .c, .h, .cc 后缀
        match = re.search(r'===\s*FILES START\s*=== \n(.*?)\n===\s*FILES END\s*===', file_response_text,
                          re.DOTALL | re.IGNORECASE)
        if match:
            raw_files = match.group(1).split('\n')
            extracted_10_files = [f.strip() for f in raw_files if
                                  f.strip() and any(ext in f for ext in ['.c', '.h', '.cc'])]
        else:
            paths = re.findall(r'([a-zA-Z0-9_/\-\.]+\.(?:cpp|cc|c|h|hpp))', file_response_text)
            extracted_10_files = list(dict.fromkeys(paths))[-10:]
    except Exception as e:
        print(f"\n❌ 批量抽取文件异常: {str(e)}")
        # 兜底：直接取 SBFL 前 10
        extracted_10_files = list(top_100_files)[:10]

    print(f"🎯 成功锁定 Top 10 可疑文件:\n" + "\n".join([f"  {i + 1}. {f}" for i, f in enumerate(extracted_10_files)]))

    # ==========================================
    # 🌟 阶段三：GCC 函数下钻循环 (含防逃逸拦截与保存)
    # ==========================================
    all_functions_data = []
    for file_rank, suspect_file_path in enumerate(extracted_10_files, 1):
        print(f"\n🔄 当前排查进度：文件 {file_rank} / 10 -> `{suspect_file_path}`")

        # 路径容错
        full_suspect_path = os.path.join(compilerDir, suspect_file_path)
        if not os.path.exists(full_suspect_path):
            for r, d, f_in in os.walk(compilerDir):
                if suspect_file_path in f_in or os.path.basename(suspect_file_path) in f_in:
                    full_suspect_path = os.path.join(r, os.path.basename(suspect_file_path))
                    break

        if not os.path.exists(full_suspect_path):
            continue

        # 执行针对 GCC 的 AST 切片和摘要提纯
        function_slice(bugid, configPath, full_suspect_path)
        generate_function_summaries(bugid, configPath, full_suspect_path)

        filename_only = os.path.basename(suspect_file_path)
        no_body_path = os.path.join(fileListDir_bugid, f"{filename_only}_no_body.md")
        with_body_path = os.path.join(fileListDir_bugid, f"{filename_only}_with_body.md")

        functions_no_body_content = read_file_content(no_body_path)
        functions_with_body_content = read_file_content(with_body_path)

        if not functions_no_body_content or "⚠️" in functions_no_body_content:
            continue

        sig_to_code = {}
        matches = re.finditer(r'### 📍 函数签名: `(.*?)`\n\n```cpp\n(.*?)\n```', functions_with_body_content, re.DOTALL)
        for m in matches:
            sig_to_code[m.group(1).strip()] = m.group(2).strip()

        drill_system_prompt = (
            "你是一个资深的 GCC 编译器缺陷下钻专家。我们将对 GCC 可疑文件进行深度函数代码审查。\n"
            "你会基于已知的根因分析上下文，挑选函数索要源码，并进行 0-10 分的打分。\n"
            "请严格按照表格格式输出打分，并仅输出下一步要求的函数签名。"
        )
        drill_messages = [{"role": "system", "content": drill_system_prompt}]
        drill_messages.append(
            {"role": "user", "content": f"【根因指南】\n{root_cause_report}\n当前文件：`{suspect_file_path}`"})
        drill_messages.append({"role": "assistant", "content": "收到，请提供 GCC 函数概览。"})

        round_funcs_for_this_file = []
        explored_sigs = set()

        # 🌟 恢复：计算文件总函数量，设置智能与保底轮次
        total_funcs_in_file = len(sig_to_code)
        MIN_EXPLORE_ROUNDS = min(30, total_funcs_in_file)
        MAX_FUNC_ROUNDS = 30
        requested_sig = ""

        print(f"\n🔍 开启文件 {filename_only} 的内部函数下钻排查 (最少 {MIN_EXPLORE_ROUNDS} 轮)...")

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
            else:
                # 🌟 恢复：防逃逸拦截系统
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
                source = sig_to_code.get(requested_sig)

                # 🌟 恢复：模糊匹配兜底，防止 LLM 吐出的签名有格式差异
                if not source:
                    norm_req_sig = normalize_sig(requested_sig)
                    for s, c in sig_to_code.items():
                        norm_s = normalize_sig(s)
                        if requested_sig in s or s in requested_sig or norm_req_sig in norm_s or norm_s in norm_req_sig:
                            source, requested_sig = c, s
                            break

                if not source:
                    source = f"⚠️ 严重提示：未找到匹配 `{requested_sig}` 的源码。请重新检查概览，如果无可疑项，请输出 [END_FILE_ANALYSIS]。"

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
                {source}
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

            try:
                print(f"  └─ 🕵️‍♂️ [R-{func_round_idx}] 正在分析...\n  思考过程: \n", end="", flush=True)
                res = client.chat.completions.create(model="deepseek-reasoner", messages=drill_messages, stream=True)

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

                tables = extract_table_from_llm_response(reply_text)
                if tables:
                    round_funcs_for_this_file.extend(tables)
                    print(f"    🌟 该函数打分: {tables[0]['llm_score']}")

                lines = [l.strip() for l in reply_text.split('\n') if l.strip()]
                requested_sig = lines[-1].replace('`', '')
            except Exception as e:
                print(f"\n  ❌ 源码审查异常: {e}")
                break

        # 🌟 恢复：单文件下钻结束，独立保存当前文件的排名
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
    # 🏁 终极汇总生成榜单 (双重榜单对比输出)
    # ==========================================
    if not all_functions_data:
        print("\n❌ 严重警告：全局排查未能提取到任何有效的函数打分。")
        return

    print("\n📈 正在进行全局综合分数计算及双榜单生成...")
    gamma = 0.85
    for item in all_functions_data:
        item['final_score'] = round(item['llm_score'] * (gamma ** (item['file_rank'] - 1)), 4)

    # ------------------------------------------
    # 榜单 1: 衰减重排 (考虑文件级优先级)
    # ------------------------------------------
    decay_sorted = sorted(all_functions_data, key=lambda x: x['final_score'], reverse=True)
    decay_ranked_md_path = os.path.join(resultDir, 'ultimate_function_rank_decay.md')
    with open(decay_ranked_md_path, 'w', encoding='utf-8') as f:
        f.write("# 🎯 GCC 全局函数缺陷定位 (指数衰减)\n\n")
        f.write(f"> **参数说明**: 基础衰减因子 Gamma = {gamma} \n")
        f.write(f"> **计算公式**: Final_Score = LLM_Score * (Gamma ^ (File_Rank - 1))\n\n")
        f.write("| 排名 | 综合得分 | 所在文件 (Rank) | 函数名称 | 原始分 |\n|---|---|---|---|---|\n")
        for idx, item in enumerate(decay_sorted, 1):
            f.write(
                f"| {idx} | **{item['final_score']}** | {os.path.basename(item['file_name'])} (R{item['file_rank']}) | `{item['func_name']}` | {item['llm_score']} |\n")

    # ------------------------------------------
    # 🌟 恢复：榜单 2：仅基于纯函数可疑度排序 (剥离文件影响)
    # ------------------------------------------
    raw_sorted_data = sorted(all_functions_data, key=lambda x: (x['llm_score'], -x['file_rank']), reverse=True)
    raw_ranked_md_path = os.path.join(resultDir, 'ultimate_function_rank_raw.md')
    with open(raw_ranked_md_path, 'w', encoding='utf-8') as f:
        f.write("# 🎯 GCC 全局函数缺陷定位 (纯函数可疑度排行)\n\n")
        f.write("> **说明**: 仅根据 LLM 对函数的直接打分 (0-10) 进行排序。当分数相同时，原文件排名更靠前的优先。\n\n")
        f.write("| 排名 | 原始 LLM 评分 | 所在文件 (Rank) | 函数名称 | 综合得分 (参考) |\n|---|---|---|---|---|\n")
        for idx, item in enumerate(raw_sorted_data, 1):
            f.write(
                f"| {idx} | **{item['llm_score']}** | {os.path.basename(item['file_name'])} (R{item['file_rank']}) | `{item['func_name']}` | {item['final_score']} |\n")

    print(f"🎉 全部分析排查完成！已生成双重对比榜单：")
    print(f"  📂 1. 指数衰减排行: {decay_ranked_md_path}")
    print(f"  📂 2. 纯函数打分排行: {raw_ranked_md_path}")