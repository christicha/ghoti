import os
import re
import json
import shutil  # 用于彻底清空文件夹
from configparser import ConfigParser
from openai import OpenAI

from llvm.function_slice import function_slice, generate_function_summaries


def get_mutant_logs_content(mutants_log_dir, case_type, limit=5):
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
    bug_report_path = os.path.join(baseLogDir, bugid, 'bug_report.txt')
    log_path = os.path.join('/home/chris/ghoti/benchmark/llvmbugs', bugid, 'version')
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
    sbfl_results = read_file_content(sbfl_result)
    abstract_content = read_file_content(summary_path)

    print("📂 正在提取并压缩变异用例的编译分析日志...")
    passing_logs_content = get_mutant_logs_content(mutants_log_dir, "pass", limit=5)
    failing_logs_content = get_mutant_logs_content(mutants_log_dir, "fail", limit=5)

    client = OpenAI(api_key=api_key, base_url=api_url, timeout=3600)

    # ==========================================
    # 🌟 全局上下文架构初始化
    # ==========================================
    system_prompt = (
        "你是一个资深的 LLVM 编译器优化缺陷分析专家。我们正在进行一个为期 20 轮的交互式缺陷定位任务。\n"
        "流程严格按照以下模式交替进行：\n"
        "1. 锁定文件：基于全局上下文，分析并选出一个最可疑的源文件。\n"
        "2. 两级下钻：先提供文件函数概览，你需要指定具体函数要求查看源码，我们再提供源码供你打分。如此循环最多 10 次。\n\n"
        "请严格遵守每轮的输出格式要求，不要输出任何多余的寒暄。"
    )

    # messages 将作为唯一的全局上下文，仅保存 System Prompt 和 第 1 轮的 Root Cause 问答
    messages = [{"role": "system", "content": system_prompt}]

    analyzed_files_list = []
    all_functions_data = []

    # 提前声明精简版 Prompt，供后续下钻函数时替换使用
    first_round_prompt_lite = ""

    print(f"\n🚀 开始执行 LLVM 缺陷定位 (BugID: {bugid}) - 20 轮文件分析流程")

    # ==========================================
    # 🌟 第一层核心：20 轮文件锁定循环
    # ==========================================
    for file_round_idx in range(1, 21):
        print(f"\n==========================================")
        print(f"🔄 当前进度：文件锁定阶段 - 第 {file_round_idx} 轮 / 共 20 轮")
        print(f"==========================================")

        # ----------------------------------------
        # 步骤 A：请求模型锁定一个文件
        # ----------------------------------------
        if file_round_idx == 1:
            # 完整版（包含 abstract.txt），永远保存在全局 messages 中用于后续挑选文件！
            prompt_file_full = f"""
            【当前进度：第 1 轮 / 共 20 轮 - 阶段：锁定可疑文件】
            请基于以下多源信息，深度分析导致该失败测试用例的编译器 Bug 根因：
            1. === Bug 报告 ===\n{bug_report}...
            2. === 原始缺陷代码在不同优化等级下的编译日志差异 ===\n{deep_compile_content}...
            3. === 编译器源文件执行次数与功能摘要 (abstract.txt) ===\n{abstract_content}...
            4. === SBFL 频谱定位结果 === \n{sbfl_results[:2500]}
            5. === 成功变异体的编译与指令突变日志 (参考) ===\n{passing_logs_content}
            6. === 失败变异体的编译与指令突变日志 (重点分析) ===\n{failing_logs_content}
            """

            # 精简版（去除 abstract.txt），专门用于稍后进入函数下钻时动态替换，以节省 Token
            first_round_prompt_lite = f"""
            【初始分析基准】
            以下是引发问题的核心背景信息（已滤除全局冗余摘要）：
            1. === Bug 报告 ===\n{bug_report}...
            2. === 原始缺陷代码在不同优化等级下的编译日志差异 ===\n{deep_compile_content}...
            3. === SBFL 频谱定位结果 === \n{sbfl_results[:2500]}
            4. === 成功变异体的编译与指令突变日志 (参考) ===\n{passing_logs_content}
            5. === 失败变异体的编译与指令突变日志 (重点分析) ===\n{failing_logs_content}
            """

            # 【全局记忆】：完整问题（含 abstract）进入全局记忆，用于支撑后续所有的文件挑选！
            messages.append({"role": "user", "content": prompt_file_full})
            print(f"🧠 正在思考并建立全局 Bug 根因推理...")
            try:
                response = client.chat.completions.create(
                    model="deepseek-reasoner",
                    messages=messages,
                )
                message_obj = response.choices[0].message
                response_file = message_obj.content or getattr(message_obj, 'reasoning_content', "")

                if not response_file: raise ValueError("API 返回内容为空")

                # 【全局记忆】：保留大模型的第一轮回答（含根因推断）在全局记忆中
                messages.append({"role": "assistant", "content": response_file})
            except Exception as e:
                print(f"❌ 锁定文件异常: {str(e)}")
                messages.pop()  # 弹出失败的问题，避免死锁
                continue

        else:
            prompt_file = f"""
            【当前进度：第 {file_round_idx} 轮 / 共 20 轮 - 阶段：锁定下一个可疑文件】
            已排查过的文件：{", ".join(analyzed_files_list)}
            
            【你的任务】
            请回顾最开始提供的全局上下文与根因推理，结合我们已经排查过的文件，重新选出第 {file_round_idx} 个最有可能存在缺陷的源文件。
            完成推理后，在回复的最后一行，仅且只输出新锁定的文件路径，绝对不能是已经排查过的文件。
            """
            # 【全局记忆复用】：后续挑文件时，带上临时 Prompt。由于 messages[1] 没变，它仍然能看到所有 abstract.txt！
            temp_file_messages = messages + [{"role": "user", "content": prompt_file}]
            print(f"🧠 基于全局根因和文件摘要，思考并锁定下一个文件...")
            try:
                response = client.chat.completions.create(
                    model="deepseek-reasoner",
                    messages=temp_file_messages,
                )
                response_file = response.choices[0].message.content or getattr(response.choices[0].message,
                                                                               'reasoning_content', "")
            except Exception as e:
                print(f"❌ 锁定文件异常: {str(e)}")
                continue

        # 提取文件路径
        suspect_file_path = ""
        for line in reversed(response_file.split('\n')):
            cleaned_line = line.replace('`', '').replace('*', '').strip()
            match = re.search(r'([a-zA-Z0-9_/\-\.]+\.(?:cpp|c|h|hpp))', cleaned_line)
            if match:
                suspect_file_path = match.group(1)
                break

        if not suspect_file_path:
            print(f"❌ 无法从大模型的回复中提取到有效的文件路径！强制跳过本轮。")
            continue

        analyzed_files_list.append(suspect_file_path)
        print(f"🎯 成功锁定可疑文件: {suspect_file_path}")

        # ----------------------------------------
        # 步骤 B：执行 AST 切割与两份 LLM 切片生成
        # ----------------------------------------
        full_suspect_path = os.path.join(compilerDir, suspect_file_path)
        print("🔪 正在执行 AST 切片与摘要提纯 (生成无代码与带代码双版本)...")
        function_slice(bugid, configPath, full_suspect_path)
        generate_function_summaries(bugid, configPath, full_suspect_path)

        # ----------------------------------------
        # 步骤 C & D 重构：10 轮函数级深度下钻
        # ----------------------------------------
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
            print(f"⏭️ 该文件 {filename_only} 无有效函数切片或已全过滤，跳入下一轮文件。")
            continue

        # 建立签名到源码的快速查询字典
        sig_to_code = {}
        matches = re.finditer(r'### 📍 函数签名: `(.*?)`\n\n```cpp\n(.*?)\n```', functions_with_body_content, re.DOTALL)
        for m in matches:
            sig_to_code[m.group(1).strip()] = m.group(2).strip()

        # 【局部记忆核心：按需卸载冗余 Token】
        drill_messages = []
        for i, msg in enumerate(messages):
            if i == 1 and msg["role"] == "user":
                drill_messages.append({"role": "user", "content": first_round_prompt_lite})
            else:
                drill_messages.append(msg.copy())

        round_funcs_for_this_file = []
        requested_sig = ""

        print(f"\n🔍 开启文件 {filename_only} 的内部函数下钻排查 (最多 10 轮)...")

        # ==========================================
        # 🌀 第二层核心：10 轮内层循环 (请求签名 -> 提供代码 -> 打分)
        # ==========================================
        for func_round_idx in range(1, 11):
            if func_round_idx == 1:
                prompt_function_overview = f"""
                【当前进度：第 {file_round_idx} 轮文件排查 - 阶段：函数概览与深度下钻 (1/10)】
                我们已锁定当前可疑文件：`{suspect_file_path}`
                该文件的整体功能摘要为：`{file_summary}`
                
                以下是该文件在处理失败测试用例时实际执行过的函数概览（仅包含函数签名和功能摘要）：
                === 函数概览 ===
                {functions_no_body_content}
                ===============
                【你的任务】
                我们需要对该文件进行最多 10 轮的深入分析。在这一轮中，请基于之前的 Bug 根因推理以及上述函数概览，挑选出**最可疑的 1 个函数**，并要求查看其完整源码。
                【输出规范】
                请简要说明（1-2句话）你选择该函数的理由。然后，在回复的最后一行，**仅且只输出**你想要查看的完整函数签名（必须与概览中提供的签名完全一致），不要带任何反引号或额外字符。
                """
                drill_messages.append({"role": "user", "content": prompt_function_overview})

                print(f"  └─ 🕵️‍♂️ [R-1] 正在请求函数概览并挑选首个嫌疑函数...")
                try:
                    res = client.chat.completions.create(model="deepseek-reasoner", messages=drill_messages)
                    reply_text = res.choices[0].message.content or getattr(res.choices[0].message, 'reasoning_content',
                                                                           "")

                    drill_messages.append({"role": "assistant", "content": reply_text})

                    lines = [l.strip() for l in reply_text.split('\n') if l.strip()]
                    requested_sig = lines[-1].replace('`', '')
                except Exception as e:
                    print(f"  ❌ 第一轮索要签名异常: {e}")
                    break

            else:
                if "[END_FILE_ANALYSIS]" in requested_sig:
                    print(f"  ✅ 模型判定该文件 {filename_only} 排查完毕，提前结束下钻。")
                    break

                # 1. 尝试精确匹配
                requested_function_source_code = sig_to_code.get(requested_sig)

                # 2. 模糊匹配兜底 (完美防空格/不可见字符幻觉版)
                if not requested_function_source_code:
                    norm_req_sig = normalize_sig(requested_sig)

                    for s, c in sig_to_code.items():
                        norm_s = normalize_sig(s)
                        # 容错 1: 简单的子串包含关系
                        if requested_sig in s or s in requested_sig:
                            requested_function_source_code = c
                            requested_sig = s
                            break
                        # 容错 2: 忽略所有空格后的包含关系 (解决指针、引用格式差异)
                        elif norm_req_sig in norm_s or norm_s in norm_req_sig:
                            requested_function_source_code = c
                            requested_sig = s
                            break

                if not requested_function_source_code:
                    requested_function_source_code = f"⚠️ 严重提示：在切片中未找到精确匹配 `{requested_sig}` 的源码。请重新检查概览，并仅输出那里存在的函数签名。如果无可疑项，请输出 [END_FILE_ANALYSIS]。"

                prompt_function_drill_down = f"""
                【当前进度：第 {file_round_idx} 轮文件排查 - 阶段：源码分析与下钻 ({func_round_idx}/10)】
                你请求查看的函数完整源码如下：
                === 函数实现 ===
                {requested_function_source_code}
                ===============
                【你的任务】
                1. **源码分析与打分**：仔细审查上述源码，判断其内部逻辑是否导致了当前的编译优化缺陷。请为该函数分配一个可疑度分数（0-10 分，可以有小数，分数越高越可疑）。
                2. **索要下一个函数**：结合已查看的函数，从之前的【函数概览】中挑选出**下一个**最可疑的函数，要求查看其源码。
                【输出规范】
                请严格按照以下 Markdown 表格格式输出对当前函数的打分结果（表格之后可以写简短的索要理由）：
                | Rank (当前下钻轮次) | Function Name | Score |
                |---|---|---|
                | {func_round_idx} | [当前查看的函数名] | [你的打分] |
                然后，在回复的最后一行，**仅且只输出**你下一步想要查看的函数签名。如果决定结束当前文件的排查，请在最后一行输出关键字：`[END_FILE_ANALYSIS]`。
                """
                drill_messages.append({"role": "user", "content": prompt_function_drill_down})

                print(f"  └─ 🔬 [{func_round_idx}/10] 发送源码 `{requested_sig}` 进行审查打分...")
                try:
                    res = client.chat.completions.create(model="deepseek-reasoner", messages=drill_messages)
                    reply_text = res.choices[0].message.content or getattr(res.choices[0].message, 'reasoning_content',
                                                                           "")

                    drill_messages.append({"role": "assistant", "content": reply_text})

                    extracted_tables = extract_table_from_llm_response(reply_text)
                    if extracted_tables:
                        round_funcs_for_this_file.extend(extracted_tables)
                        print(f"    🌟 该函数打分: {extracted_tables[0]['llm_score']}")

                    lines = [l.strip() for l in reply_text.split('\n') if l.strip()]
                    requested_sig = lines[-1].replace('`', '')

                    if "[END_FILE_ANALYSIS]" in requested_sig or "[END_FILE_ANALYSIS]" in reply_text:
                        print(f"  ✅ 模型输出终止符，文件 {filename_only} 内部排查结束。")
                        break
                except Exception as e:
                    print(f"  ❌ 源码审查轮次异常: {e}")
                    break

        # ----------- 单文件排查结束，保存该文件的函数分数 -----------
        if round_funcs_for_this_file:
            single_round_md_path = os.path.join(resultDir, f"round_{file_round_idx}_{filename_only}_rank.md")
            with open(single_round_md_path, 'w', encoding='utf-8') as srf:
                srf.write(f"# 第 {file_round_idx} 轮文件函数排名 - `{filename_only}`\n\n")
                srf.write("| 探测顺序 | Function Name | Score |\n")
                srf.write("|---|---|---|\n")
                for idx, func in enumerate(round_funcs_for_this_file, 1):
                    srf.write(f"| {idx} | `{func['func_name']}` | {func['llm_score']} |\n")

            for func in round_funcs_for_this_file:
                func['file_rank'] = file_round_idx
                func['file_name'] = suspect_file_path
                all_functions_data.append(func)

            print(
                f"💾 第 {file_round_idx} 轮单文件 ({len(round_funcs_for_this_file)} 个函数) 排行已独立保存: {single_round_md_path}")
        else:
            print(f"⚠️ 第 {file_round_idx} 轮未提取到任何函数打分。")

    # ==========================================
    # 🏁 终极汇总与指数衰减重新排版
    # ==========================================
    if not all_functions_data:
        print("\n❌ 严重警告：20 轮排查未能提取到任何有效的函数打分。")
        return

    print("\n📈 正在进行全局综合分数计算 (指数衰减策略)...")
    gamma = 0.85

    for item in all_functions_data:
        file_rank = item['file_rank']
        llm_score = item['llm_score']
        item['final_score'] = round(llm_score * (gamma ** (file_rank - 1)), 4)

    all_functions_data.sort(key=lambda x: x['final_score'], reverse=True)

    final_ranked_md_path = os.path.join(resultDir, 'ultimate_function_rank.md')
    md_content = f"# 🎯 BugID: {bugid} - 全局函数级缺陷定位最终排行\n\n"
    md_content += f"> **参数说明**: 基础衰减因子 Gamma = {gamma} \n"
    md_content += f"> **计算公式**: Final_Score = LLM_Score * (Gamma ^ (File_Rank - 1))\n\n"
    md_content += "| 全局排名 | 综合得分 | 所在文件 (Rank) | 函数名称 | 原始 LLM 评分 |\n"
    md_content += "|:---:|:---:|---|---|:---:|\n"

    for idx, item in enumerate(all_functions_data, 1):
        md_content += f"| {idx} | **{item['final_score']}** | {os.path.basename(item['file_name'])} (R{item['file_rank']}) | `{item['func_name']}` | {item['llm_score']} |\n"

    with open(final_ranked_md_path, 'w', encoding='utf-8') as f:
        f.write(md_content)

    print(f"🎉 全部分析排查完成！终极全局排行已保存至: {final_ranked_md_path}")