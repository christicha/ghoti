import os
import json
import csv
import re
import random
from configparser import ConfigParser
from openai import OpenAI


def read_file_content(file_path):
    """辅助函数：安全读取文件内容"""
    try:
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read()
    except Exception as e:
        print(f"⚠️ 警告：无法读取文件 {file_path} - {str(e)}")
    return ""


def load_mutated_cases(case_dir):
    """辅助函数：遍历目录并拼接所有变异的 C 文件内容"""
    cases_content = []
    if os.path.exists(case_dir):
        for filename in sorted(os.listdir(case_dir)):
            if filename.endswith(".c"):
                content = read_file_content(os.path.join(case_dir, filename))
                cases_content.append(
                    f"// ====================\n// 变异文件: {filename}\n// ====================\n{content}")
    return "\n\n".join(cases_content) if cases_content else "// 未提供此类型变异测试用例"


def get_full_sbfl_with_summary(bugid, baseSBFLDir, summaryDir):
    """读取完整的 SBFL 排名，并拼接对应的 JSON 功能摘要"""
    sbflPath = os.path.join(baseSBFLDir, bugid, 'SBFL.csv')

    if not os.path.exists(sbflPath):
        print(f"❌ 找不到 SBFL 排名文件: {sbflPath}")
        return None

    print(f"🔍 正在拼接完整的 SBFL 排名与文件功能摘要...")
    combined_info_list = []

    with open(sbflPath, 'r', encoding='utf-8') as f:
        lines = f.readlines()
        for line in lines:
            line = line.strip()
            if not line or line.lower().startswith("rank"):
                continue

            parts = line.split(',')
            if len(parts) >= 2:
                rank = parts[0].strip()
                filepath = parts[1].strip()

                basename = os.path.basename(filepath)
                json_filename = f"{basename}.json"
                json_path = os.path.join(summaryDir, json_filename)

                summary_text = "⚠️ 暂无功能摘要 (未找到对应的 JSON 文件)"
                if os.path.exists(json_path):
                    try:
                        with open(json_path, 'r', encoding='utf-8') as jf:
                            data = json.load(jf)
                            summary_text = data.get("summary", "JSON 文件中无 'summary' 字段")
                    except json.JSONDecodeError:
                        summary_text = "⚠️ 读取摘要失败: JSON 格式错误"
                    except Exception as e:
                        summary_text = f"⚠️ 读取摘要失败: {str(e)}"

                info_block = (
                    f"Rank: {rank} | File: {filepath}\n"
                    f"功能摘要: {summary_text}"
                )
                combined_info_list.append(info_block)

    final_combined_text = "\n".join(combined_info_list)
    print(f"✅ 成功提取并拼接了 {len(combined_info_list)} 个文件的摘要信息。")
    return final_combined_text


def extract_ranks_from_md(md_text):
    """从 LLM 生成的 Markdown 文本中解析提取 LLM 排名和原 SBFL 排名"""
    llm_ranks = {}
    sbfl_ranks = {}

    lines = md_text.strip().split('\n')
    in_table = False

    for line in lines:
        line = line.strip()
        # 识别表格头
        if line.startswith('|') and 'Rank' in line and 'Filename' in line:
            in_table = True
            continue
        # 跳过分隔行
        if in_table and line.startswith('|') and '---' in line:
            continue

        if in_table and line.startswith('|'):
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 4:
                try:
                    # 提取 LLM 排名 (去除潜在的非数字字符)
                    llm_rank_match = re.search(r'\d+', parts[1])
                    if not llm_rank_match: continue
                    llm_rank = int(llm_rank_match.group())

                    # 提取文件名 (清理 Markdown 加粗或代码块符号)
                    filename = parts[2].replace('*', '').replace('`', '').strip()

                    # 提取 SBFL 排名
                    sbfl_rank_match = re.search(r'\d+', parts[3])
                    sbfl_rank = int(sbfl_rank_match.group()) if sbfl_rank_match else 9999

                    llm_ranks[filename] = llm_rank
                    sbfl_ranks[filename] = sbfl_rank
                except Exception:
                    continue
        elif in_table and not line.startswith('|'):
            # 表格结束
            in_table = False

    return llm_ranks, sbfl_ranks


def monte_carlo_rank_aggregation(llm_ranks, sbfl_ranks, num_simulations=10000):
    """
    使用蒙特卡洛模拟算法聚合排名。
    核心思想：模拟 10000 次，每次随机生成 LLM 和 SBFL 的权重比例，
    计算加权倒数排名融合（RRF）得分，最终得出期望得分并重新排序。
    """
    all_files = set(llm_ranks.keys()).union(set(sbfl_ranks.keys()))

    # 针对缺失排名的文件进行惩罚（赋予末尾排名）
    max_llm = max(llm_ranks.values()) if llm_ranks else 100
    max_sbfl = max(sbfl_ranks.values()) if sbfl_ranks else 100
    default_llm = max_llm + 10
    default_sbfl = max_sbfl + 10

    # RRF (Reciprocal Rank Fusion) 常数 k，用于平滑极端排名差
    k = 60
    final_scores = {f: 0.0 for f in all_files}

    # 蒙特卡洛模拟
    for _ in range(num_simulations):
        # 随机分配 LLM 和 SBFL 的信任权重
        w_llm = random.uniform(0, 1)
        w_sbfl = 1.0 - w_llm

        for f in all_files:
            r_llm = llm_ranks.get(f, default_llm)
            r_sbfl = sbfl_ranks.get(f, default_sbfl)

            # 计算单词模拟的加权得分
            score = w_llm * (1.0 / (k + r_llm)) + w_sbfl * (1.0 / (k + r_sbfl))
            final_scores[f] += score

    # 计算期望得分并降序排列（得分越高排名越靠前）
    sorted_files = sorted(final_scores.items(), key=lambda x: x[1], reverse=True)

    aggregated_ranking = []
    for i, (f, score) in enumerate(sorted_files):
        aggregated_ranking.append({
            "FinalRank": i + 1,
            "Filename": f,
            "LLMRank": llm_ranks.get(f, "-"),
            "SBFLRank": sbfl_ranks.get(f, "-"),
            "AggScore": round(score / num_simulations, 6)
        })

    return aggregated_ranking


def analyze_and_rank_files(bugid, configFile, rightOption, wrongOption):
    cfg = ConfigParser()
    cfg.read(configFile)

    # 1. 加载路径配置
    baseLogDir = cfg.get('llvm-locations', 'logDir')
    baseCaseDir = cfg.get('llvm-locations', 'passdir')
    baseInfoPath = cfg.get('llvm-locations', 'infodir')
    baseResultDir = cfg.get('llvm-locations', 'resultDir')
    baseSBFLDir = cfg.get('llvm-locations', 'resultDir')
    summaryDir = cfg.get('llvm-locations', 'summaryDir')

    api_key = cfg.get('deepseek', 'api')
    api_url = cfg.get('deepseek', 'url')

    print(f"\n==============================================")
    print(f"🚀 开始为 BugID: {bugid} 执行全链路缺陷定位分析")
    print(f"==============================================\n")

    logPath = os.path.join(baseLogDir, bugid, 'compile.log')
    passCaseDir = os.path.join(baseCaseDir, bugid, 'passing_cases')
    failCaseDir = os.path.join(baseCaseDir, bugid, 'failing_cases')
    ori = os.path.join(baseInfoPath, bugid, 'fail.c')

    ori_content = read_file_content(ori)
    log_content = read_file_content(logPath)
    fail_cases_str = load_mutated_cases(failCaseDir)
    pass_cases_str = load_mutated_cases(passCaseDir)

    if not ori_content or not log_content:
        print("❌ 致命错误：找不到原始 fail.c 或 compile.log 文件，中止分析。")
        return

    client = OpenAI(api_key=api_key, base_url=api_url, timeout=3600)

    # ==========================================
    # 第一轮交互：根因分析 (RCA)
    # ==========================================
    system_prompt = (
        "你是一位顶尖的 LLVM 编译器开发专家与程序分析研究员，精通 C 语言规范、未定义行为（UB）分析、"
        "编译器优化管线、中间代码（IR）转换、x86-64 汇编以及基于覆盖率的缺陷定位（SBFL）理论。"
    )

    rca_prompt = f"""任务目标
    我遇到了一起由于编译器优化等级导致的bug，编译器在{rightOption}不触发bug，在{wrongOption}触发bug。我将提供触发该 Bug 的原始代码、基于它变异的成功/失败测试用例集，以及详尽的编译阶段对比日志。请你执行深度差分分析，推导出导致该 Bug 的根因假设。

    输入数据
    1. 原始触发 Bug 的测试程序 (fail.c)
    {ori_content}

    2. 变异测试用例集 (差分数据)
    保持 Bug 触发的变异程序 (Failing Cases):
    {fail_cases_str}
    不再触发 Bug 的变异程序 (Passing Cases):
    {pass_cases_str}

    3. 深度编译分析日志 ({rightOption} vs {wrongOption})
    {log_content}

    分析指令
    第一步：差分语法与语义分析 (Test Case Delta Analysis)
    提取出导致程序从“触发 Bug”变成“不触发 Bug”的最小关键语法或语义突变。结合 Sanitizer 日志，排除由于测试用例本身存在未定义行为（UB）的可能性。
    第二步：汇编与编译日志映射 (Log-to-Code Mapping)
    分析核心指令差异，利用交织反汇编，将“关键语法突变”直接映射到被错误优化掉或生成错误的底层汇编指令上。
    第三步：编译器内部状态推导 (IR & Optimization Pass Hypothesis)
    推断是哪一类数据流分析或控制流分析得出了错误结论，提出 2-3 个最有可能的嫌疑优化 Pass。

    请以结构化的技术报告形式输出你的分析结果，必须包含以下三个标题：
    1. 触发条件隔离
    2. 根因假设分析
    3. 高度嫌疑的优化 Pass
    """

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": rca_prompt}
    ]

    print("🧠 [阶段 1/3] 正在呼叫大模型生成根因分析报告 (RCA)...")
    try:
        response_rca = client.chat.completions.create(
            model="deepseek-reasoner",
            messages=messages,
            temperature=1.0,
            max_tokens=50000
        )
        rca_report = response_rca.choices[0].message.content

        rca_report_path = os.path.join(baseLogDir, bugid, f"root_cause.md")
        with open(rca_report_path, 'w', encoding='utf-8') as f:
            f.write(rca_report)
        print(f"✅ 成功！根因分析报告已生成并保存至: {rca_report_path}")

        messages.append({"role": "assistant", "content": rca_report})

    except Exception as e:
        print(f"❌ RCA 分析调用失败: {str(e)}")
        return

    # ==========================================
    # 第二轮交互：交叉验证与重排序 (LLM)
    # ==========================================
    sbfl_summary_content = get_full_sbfl_with_summary(bugid, baseSBFLDir, summaryDir)
    if not sbfl_summary_content:
        print("❌ 获取 SBFL 与功能摘要数据失败，中止重排序。")
        return

    ranking_prompt = f"""非常好！现在基于你刚刚得出的根因分析逻辑，我们需要将范围锁定到具体的编译器源码文件。
    我将为你提供一份基于覆盖率计算的“SBFL可疑度排名”以及“被执行文件的功能摘要”。

    # 输入数据：SBFL排名及被执行文件的功能摘要
    {sbfl_summary_content}

    # 分析指令
    请你将统计学证据（SBFL）与你刚刚得出的语义学线索（RCA报告）进行深度交叉验证：
    1. 降权/剔除假阳性：如果一个文件在 SBFL 中排名很高，但其功能摘要显示它只是底层的通用数据结构或基础流程控制，且与 RCA 的核心特征毫不相干，请果断降低其嫌疑度。
    2. 提权/锁定真阳性：如果某文件的功能摘要与你刚刚指出的“嫌疑优化 Pass”及“指令破坏特征”高度吻合，请大幅提升其嫌疑度。
    3. 缺陷机理推断：针对提权后的核心文件，结合你之前描述的逻辑断层，用一两句话推断该文件内部的代码具体是如何犯错的。

    # 输出格式要求
    请以结构化的 Markdown 格式输出最终结果：
    ### 1. 缺陷领域画像
    简要总结该 Bug 所属的核心编译器模块和优化阶段。
    ### 2. 完整的可疑文件重排序列表
    请提供一个表格，必须包含以下列：
    | Rank | Filename | 原SBFL排名 | 重排序理由摘要 |
    """

    messages.append({"role": "user", "content": ranking_prompt})

    print("🧠 [阶段 2/3] 正在利用上下文和 SBFL 数据执行智能重排序...")
    try:
        response_ranking = client.chat.completions.create(
            model="deepseek-reasoner",
            messages=messages,
            temperature=1.0,
            max_tokens=50000
        )
        final_ranking_report = response_ranking.choices[0].message.content

        ranking_report_path = os.path.join(baseResultDir, bugid, 'files_rank.md')
        with open(ranking_report_path, 'w', encoding='utf-8') as out_f:
            out_f.write(final_ranking_report)
        print(f"✅ 成功！排序报告已生成并保存至: {ranking_report_path}")

    except Exception as e:
        print(f"❌ 重排序调用失败: {str(e)}")
        return

    # ==========================================
    # 第三阶段：蒙特卡洛算法聚合排名生成 CSV
    # ==========================================
    print("📊 [阶段 3/3] 正在使用蒙特卡洛模拟算法聚合 LLM 与 SBFL 的排名...")
    try:
        llm_ranks, sbfl_ranks = extract_ranks_from_md(final_ranking_report)

        if not llm_ranks:
            print("⚠️ 警告：未能从大模型输出中解析到有效的表格数据，跳过聚合步骤。")
            return

        final_ranks = monte_carlo_rank_aggregation(llm_ranks, sbfl_ranks)

        csv_path = os.path.join(baseResultDir, bugid, 'final_file_rank.csv')
        with open(csv_path, 'w', newline='', encoding='utf-8') as csvfile:
            fieldnames = ['FinalRank', 'Filename', 'LLMRank', 'SBFLRank', 'AggScore']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

            writer.writeheader()
            for row in final_ranks:
                writer.writerow(row)

        print(f"✅ 成功！最终聚合排名已保存至: {csv_path}")
        print("\n🎉 全链路分析与多维度排名合并完成！")

    except Exception as e:
        print(f"❌ 聚合分析执行失败: {str(e)}")
