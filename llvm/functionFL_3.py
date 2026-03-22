import os
import re
import json
from configparser import ConfigParser
from openai import OpenAI

from llvm.function_slice import function_slice, generate_function_summaries


# 假设我们在同一个文件中已经有了这两个函数的定义
# from your_module import function_slice, generate_function_summaries

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
            if len(parts) >= 4 and parts[1].lower() != 'rank':
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


def function_FL(bugid, configPath, rev, compilerOptionRight, compilerOptionWrong):
    cfg = ConfigParser()
    cfg.read(configPath)

    # 1. 获取路径配置
    baseLogDir = cfg.get('llvm-locations', 'logDir')
    baseResultDir = cfg.get('llvm-locations', 'resultDir')
    baseFileListDir = cfg.get('llvm-locations', 'fileListDir')
    baseCompilerDir = cfg.get('llvm-locations', 'compilersdir')
    summaryDir = cfg.get('llvm-locations', 'summaryDir')
    baseInfoDir = cfg.get('llvm-locations', 'infodir')
    api_key = cfg.get('deepseek', 'api')
    api_url = cfg.get('deepseek', 'url')

    # 定义各个文件的绝对路径
    bug_report_path = os.path.join(baseLogDir, bugid, 'bug_report.txt')
    log_path = os.path.join(baseLogDir, bugid, 'compile.log')  # 对应你的深度编译日志
    sbfl_result = os.path.join(baseLogDir, bugid, 'SBFL.csv')
    summary_path = os.path.join(baseLogDir, bugid, 'abstract_summary.txt')
    compilerDir = os.path.join(baseCompilerDir, rev, rev, 'llvm')
    resultDir = os.path.join(baseResultDir, bugid)
    os.makedirs(resultDir, exist_ok=True)

    # 2. 读取上下文数据
    bug_report = read_file_content(bug_report_path)
    deep_compile_content = read_file_content(log_path)
    sbfl_results = read_file_content(sbfl_result)
    abstract_content = read_file_content(summary_path)

    # 3. 初始化 LLM 客户端与系统提示词
    client = OpenAI(api_key=api_key, base_url=api_url, timeout=3600)

    system_prompt = (
        "你是一个资深的 LLVM 编译器优化缺陷分析专家。我们正在进行一个为期 10 轮的交互式缺陷定位任务。\n"
        "整个流程严格按照以下模式交替进行：\n"
        "1. 锁定文件：基于全局上下文（Bug报告、多优化等级编译日志差异、SBFL频谱定位、文件执行频次与功能摘要），分析并选出一个最可疑的源文件。\n"
        "2. 下钻函数：我将提供该文件中执行过的函数详情，你需要对这些函数进行可疑度打分和降序排列。\n\n"
        "请严格遵守每轮的输出格式要求，不要输出任何多余的寒暄或超出当前步骤的内容。"
    )

    messages = [{"role": "system", "content": system_prompt}]
    analyzed_files_list = []
    all_functions_data = []  # 用于存储所有轮次提取的函数打分数据

    print(f"\n🚀 开始执行 LLVM 缺陷定位 (BugID: {bugid}) - 10 轮分析流程")

    # ==========================================
    # 🌟 核心：10 轮交互循环
    # ==========================================
    for round_idx in range(1, 11):
        print(f"\n==========================================")
        print(f"🔄 当前进度：第 {round_idx} 轮 / 共 10 轮")
        print(f"==========================================")

        # ----------------------------------------
        # 步骤 A：请求模型锁定一个文件
        # ----------------------------------------
        if round_idx == 1:
            prompt_file = f"""
            【当前进度：第 1 轮 / 共 10 轮 - 阶段：锁定可疑文件】

            请基于以下多源信息，分析导致该失败测试用例的 Bug 根因：
            1. === Bug 报告 ===\n{bug_report[:2000]}...
            2. === 不同优化等级下的编译日志差异 ===\n{deep_compile_content[:6000]}...
            3. === 基于频谱的缺陷定位 (SBFL) 结果 ===\n{sbfl_results[:2000]}...
            4. === 文件执行次数与功能摘要 (abstract.txt) ===\n{abstract_content}...

            【你的任务】
            1. 深度交叉分析上述信息，特别是优化行为的差异（如 {compilerOptionRight} 与 {compilerOptionWrong} 的 Pass 差异）与执行频次、SBFL 结果的映射关系。
            2. 推理出导致该 Bug 的根本原因。
            3. 综合所有特征，选出 1 个 你认为最有可能存在缺陷的源文件。

            【输出规范】
            请先给出详细的逻辑推理过程。完成推理后，在回复的最后一行，仅且只输出该文件的绝对或相对路径，不要带任何标点、反引号或额外说明。
            """
            print(prompt_file)
        else:
            prompt_file = f"""
            【当前进度：第 {round_idx} 轮 / 共 10 轮 - 阶段：锁定下一个可疑文件】
            我们已经完成了对以下文件的函数级排查：
            {", ".join(analyzed_files_list)}

            【你的任务】
            请回顾最开始提供的全局 Bug 上下文（编译日志差异、SBFL 结果、文件频次等），结合我们已经排查过的文件，重新评估并选出第 {round_idx} 个最有可能存在缺陷的源文件。

            【输出规范】
            请给出简短的转移推理过程（为什么排除之前的，为什么选择现在的）。完成推理后，在回复的最后一行，仅且只输出新锁定的文件路径，格式与之前保持一致，且绝对不能是已经排查过的文件。
            """
            print(prompt_file)

        messages.append({"role": "user", "content": prompt_file})

        # 调用大模型获取文件名
        print(f"🧠 正在思考锁定文件...")
        response_file = \
        client.chat.completions.create(model="deepseek-reasoner", messages=messages, max_tokens=4096).choices[
            0].message.content
        messages.append({"role": "assistant", "content": response_file})

        # 解析最后一行的文件路径
        suspect_file_path = response_file.strip().split('\n')[-1].replace('`', '').strip()
        analyzed_files_list.append(suspect_file_path)
        print(f"🎯 锁定可疑文件: {suspect_file_path}")

        # ----------------------------------------
        # 步骤 B：执行 AST 切割与 LLM 过滤
        # ----------------------------------------
        # 补全完整本地路径 (依据 compilerDir 拼接)
        full_suspect_path = os.path.join(compilerDir, suspect_file_path)

        print("🔪 正在执行 AST 切片与摘要提纯...")
        function_slice(bugid, configPath, full_suspect_path)
        generate_function_summaries(bugid, configPath, full_suspect_path)

        # ----------------------------------------
        # 步骤 C：读取处理后的数据，发起下钻查询
        # ----------------------------------------
        filename_only = os.path.basename(suspect_file_path)
        filtered_md_path = os.path.join(baseFileListDir, bugid, f"{filename_only}_filtered.md")
        json_summary_path = os.path.join(summaryDir, f"{filename_only}.json")

        # 读取文件的全局摘要
        file_summary = "⚠️ 暂无全局功能摘要"
        if os.path.exists(json_summary_path):
            try:
                with open(json_summary_path, 'r', encoding='utf-8') as jf:
                    file_summary = json.load(jf).get("summary", file_summary)
            except Exception:
                pass

        # 读取被保留下来的函数切片代码和摘要
        functions_data_str = read_file_content(filtered_md_path)
        if not functions_data_str or "⚠️" in functions_data_str:
            functions_data_str = "该文件没有有效的函数切片数据或被全过滤。"

        prompt_analyze_functions = f"""
        【当前进度：第 {round_idx} 轮 / 共 10 轮 - 阶段：下钻函数分析】

        我们已锁定当前可疑文件：`{suspect_file_path}`
        该文件的整体功能摘要为：`{file_summary}`

        以下是该文件在处理失败测试用例时实际执行过的函数列表（包含函数签名、代码实现及摘要）：
        === 函数数据 ===
        {functions_data_str}
        ===============

        【你的任务】
        结合我们之前推理出的 Bug 根因，对上述函数进行深度排查：
        1. 评估每个函数的内部逻辑是否可能导致当前的编译优化缺陷。
        2. 为每个函数分配可疑度分数（0-10分，分数越高越可疑）。
        3. 根据分数对所有函数进行降序排列。

        【输出规范】
        请先简要分析最可疑的几个函数，随后必须严格按照以下 Markdown 表格格式输出所有函数的排序结果：

        | Rank | Function Name | Score |
        |---|---|---|
        | 1 | [函数名] | [分数] |
        """
        print(prompt_analyze_functions)

        # ----------------------------------------
        # 🔥【关键优化】：使用临时上下文，阅后即焚，不污染全局 messages！
        # ----------------------------------------
        temp_messages = messages + [{"role": "user", "content": prompt_analyze_functions}]

        print(f"🔬 正在对文件内函数进行评估打分...")
        response_funcs = client.chat.completions.create(
            model="deepseek-reasoner",
            messages=temp_messages,
            max_tokens=4096
        ).choices[0].message.content

        # 注意：这里我们故意省略了 messages.append(assistant_response) 来保持主干的干净

        # ----------------------------------------
        # 步骤 D：解析提取表格并赋予 File_Rank，且单独保存单文件表格
        # ----------------------------------------
        round_funcs = extract_table_from_llm_response(response_funcs)

        # ✅ 【新增功能】：将每一轮的单文件函数排名结果单独保存
        single_round_md_path = os.path.join(resultDir, f"round_{round_idx}_{filename_only}_rank.md")
        with open(single_round_md_path, 'w', encoding='utf-8') as srf:
            srf.write(f"# 第 {round_idx} 轮函数排名 - `{filename_only}`\n\n")
            srf.write("| Rank | Function Name | Score |\n")
            srf.write("|---|---|---|\n")
            for idx, func in enumerate(round_funcs, 1):
                srf.write(f"| {idx} | `{func['func_name']}` | {func['llm_score']} |\n")

        print(f"💾 第 {round_idx} 轮单文件排行已独立保存: {single_round_md_path}")

        # 将数据录入全局池
        for func in round_funcs:
            func['file_rank'] = round_idx
            func['file_name'] = suspect_file_path
            all_functions_data.append(func)

        print(f"📊 第 {round_idx} 轮完成，提取了 {len(round_funcs)} 个有效函数评分。")

    # ==========================================
    # 🏁 汇总与指数衰减重新排版
    # ==========================================
    print("\n📈 正在进行全局综合分数计算 (指数衰减策略)...")
    gamma = 0.85

    for item in all_functions_data:
        file_rank = item['file_rank']
        llm_score = item['llm_score']
        # 核心加权计算
        item['final_score'] = round(llm_score * (gamma ** (file_rank - 1)), 4)

    # 按照综合得分进行全局降序排序
    all_functions_data.sort(key=lambda x: x['final_score'], reverse=True)

    # 生成终极重排版 Markdown
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

    print(f"🎉 全部 10 轮排查完成！终极全局排行已保存至: {final_ranked_md_path}")