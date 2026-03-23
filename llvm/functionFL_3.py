import os
import re
import json
import shutil  # ✅ 用于彻底清空文件夹
from configparser import ConfigParser
from openai import OpenAI

from llvm.function_slice import function_slice, generate_function_summaries


def empty_directory(folder_path):
    """✅ 清空指定文件夹下的所有内容，但保留文件夹本身"""
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


def get_test_cases_content(dir_path, limit=5):
    """从指定目录中读取前 N 个测试用例文件的内容"""
    if not os.path.exists(dir_path):
        return "⚠️ 目录不存在或无测试用例"

    # 获取目录下所有的文件，并排序保证每次读取的顺序一致
    files = [f for f in os.listdir(dir_path) if os.path.isfile(os.path.join(dir_path, f))]
    files.sort()

    if not files:
        return "⚠️ 该目录下暂无文件"

    content_list = []
    for f in files[:limit]:
        file_path = os.path.join(dir_path, f)
        content = read_file_content(file_path)
        # 用明显的分隔符把各个用例隔开
        content_list.append(f"// ======== Test Case: {f} ========\n{content}")

    return "\n\n".join(content_list)


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
    basePassDir = cfg.get('llvm-locations', 'passdir')
    summaryDir = cfg.get('llvm-locations', 'summaryDir')
    baseInfoDir = cfg.get('llvm-locations', 'infodir')
    api_key = cfg.get('deepseek', 'api')
    api_url = cfg.get('deepseek', 'url')

    # 定义各个文件的绝对路径
    bug_report_path = os.path.join(baseLogDir, bugid, 'bug_report.txt')
    log_path = os.path.join('/home/chris/ghoti/benchmark/llvmbugs', bugid, 'version')
    sbfl_result = os.path.join(baseLogDir, bugid, 'SBFL.csv')
    summary_path = os.path.join(baseLogDir, bugid, 'abstract_summary.txt')
    compilerDir = os.path.join(baseCompilerDir, rev, rev, 'llvm')

    passDir = os.path.join(basePassDir, bugid, 'passing_case')
    failDir = os.path.join(basePassDir, bugid, 'failing_case')

    resultDir = os.path.join(baseResultDir, bugid)
    fileListDir_bugid = os.path.join(baseFileListDir, bugid)

    # ==========================================
    # ✅ 在一切开始前，强制清空历史遗留的缓存文件
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

    # 2. 读取上下文数据
    bug_report = read_file_content(bug_report_path)
    deep_compile_content = read_file_content(log_path)
    sbfl_results = read_file_content(sbfl_result)
    abstract_content = read_file_content(summary_path)

    # 提前读取成功/失败变异测试用例
    passing_cases_content = get_test_cases_content(passDir, limit=5)
    failing_cases_content = get_test_cases_content(failDir, limit=5)

    # 3. 初始化 LLM 客户端与系统提示词
    client = OpenAI(api_key=api_key, base_url=api_url, timeout=3600)

    system_prompt = (
        "你是一个资深的 LLVM 编译器优化缺陷分析专家。我们正在进行一个为期 20 轮的交互式缺陷定位任务。\n"
        "整个流程严格按照以下模式交替进行：\n"
        "1. 锁定文件：基于全局上下文（Bug报告、多优化等级编译日志差异、SBFL频谱定位、文件执行频次与功能摘要），分析并选出一个最可疑的源文件。\n"
        "2. 下钻函数：我将提供该文件中执行过的函数详情，你需要对这些函数进行可疑度打分和降序排列。\n\n"
        "请严格遵守每轮的输出格式要求，不要输出任何多余的寒暄或超出当前步骤的内容。"
    )

    messages = [{"role": "system", "content": system_prompt}]
    analyzed_files_list = []
    all_functions_data = []  # 用于存储所有轮次提取的函数打分数据

    print(f"\n🚀 开始执行 LLVM 缺陷定位 (BugID: {bugid}) - 20 轮分析流程")

    # ==========================================
    # 🌟 核心：20 轮交互循环
    # ==========================================
    for round_idx in range(1, 21):
        print(f"\n==========================================")
        print(f"🔄 当前进度：第 {round_idx} 轮 / 共 20 轮")
        print(f"==========================================")

        # ----------------------------------------
        # 步骤 A：请求模型锁定一个文件
        # ----------------------------------------
        if round_idx == 1:
            # 🔥 完整版 Prompt (仅发给模型进行第一次深度思考，包含极耗 Token 的摘要)
            prompt_file_full = f"""
            【当前进度：第 1 轮 / 共 20 轮 - 阶段：锁定可疑文件】

            请基于以下多源信息，分析导致该失败测试用例的 Bug 根因：
            1. === Bug 报告 ===\n{bug_report}...
            2. === 不同优化等级下的编译日志差异 ===\n{deep_compile_content}...
            3. === 文件执行次数与功能摘要 (abstract.txt) ===\n{abstract_content}...
            4. === 变异的成功测试用例 (参考) ===\n{passing_cases_content}
            5. === 变异的失败测试用例 (分析) ===\n{failing_cases_content}
            6. === SBFL结果 === \n{sbfl_results[:2500]}

            【你的任务】
            1. 深度交叉分析上述信息，特别是优化行为的差异（如 {compilerOptionRight} 与 {compilerOptionWrong} 的 Pass 差异）与执行频次、文件功能摘要。
            2. 结合变异测试用例中成功与失败的代码差异（重点关注哪些语法或结构导致了崩溃），推理出导致该 Bug 的根本原因。
            3. 综合所有特征，选出 1 个 你认为最有可能存在缺陷的源文件。

            【输出规范】
            请先给出详细的逻辑推理过程。完成推理后，在回复的最后一行，仅且只输出该文件的绝对或相对路径，不要带任何标点、反引号或额外说明。
            """

            # 🍃 精简版 Prompt (去除了摘要，用于长久保存在全局上下文中，极大节省 Token)
            prompt_file_lite = f"""
            【初始分析基准】
            以下是引发问题的核心背景信息：
            1. === Bug 报告 ===\n{bug_report}...
            2. === 不同优化等级下的编译日志差异 ===\n{deep_compile_content}...
            3. === 变异的成功测试用例 (参考) ===\n{passing_cases_content}
            4. === 变异的失败测试用例 (分析) ===\n{failing_cases_content}
            5. === SBFL结果 === \n{sbfl_results[:2500]}
            """

            # 使用完整版提问
            messages.append({"role": "user", "content": prompt_file_full})

            print(f"🧠 正在思考锁定文件...")
            try:
                response = client.chat.completions.create(
                    model="deepseek-reasoner",
                    messages=messages,
                    max_tokens=35000
                )
                message_obj = response.choices[0].message
                response_file = message_obj.content or ""

                # 兜底：如果正式结论为空，尝试提取内部思维链
                reasoning = getattr(message_obj, 'reasoning_content', "")
                if not response_file and reasoning:
                    print("⚠️ 模型正式结论为空，尝试提取内部思维链...")
                    response_file = reasoning

                if not response_file:
                    raise ValueError("API 返回内容完全为空")

                # 🔥 移花接木：将发出去的完整版 User Prompt 替换为去除了摘要的精简版
                messages[-1] = {"role": "user", "content": prompt_file_lite}

                # 将第 1 轮的绝佳推理完整加入全局上下文
                messages.append({"role": "assistant", "content": response_file})

            except Exception as e:
                print(f"❌ 请求模型锁定文件时发生异常: {str(e)}")
                print("⏭️ 放弃当前提问，重置历史并跳过本轮...")
                messages.pop()  # 把失败的 prompt 弹出去，防止阻碍下一轮
                continue

        else:
            prompt_file = f"""
            【当前进度：第 {round_idx} 轮 / 共 20 轮 - 阶段：锁定下一个可疑文件】
            我们已经完成了对以下文件的函数级排查：
            {", ".join(analyzed_files_list)}

            【你的任务】
            请回顾最开始提供的全局 Bug 上下文（编译日志差异、SBFL 结果、文件频次、测试用例对比等），结合我们已经排查过的文件，重新评估并选出第 {round_idx} 个最有可能存在缺陷的源文件。

            【输出规范】
            请给出简短的转移推理过程（为什么排除之前的，为什么选择现在的）。完成推理后，在回复的最后一行，仅且只输出新锁定的文件路径，格式与之前保持一致，且绝对不能是已经排查过的文件。
            """

            # 🔥 阅后即焚：使用临时上下文，不污染全局 messages
            temp_file_messages = messages + [{"role": "user", "content": prompt_file}]

            print(f"🧠 正在思考锁定文件...")
            try:
                response = client.chat.completions.create(
                    model="deepseek-reasoner",
                    messages=temp_file_messages,
                    max_tokens=40000
                )
                message_obj = response.choices[0].message
                response_file = message_obj.content or ""

                reasoning = getattr(message_obj, 'reasoning_content', "")
                if not response_file and reasoning:
                    print("⚠️ 模型正式结论为空，尝试提取内部思维链...")
                    response_file = reasoning

                if not response_file:
                    raise ValueError("API 返回内容完全为空")

                # 🛑 绝对不要把 response_file append 进全局 messages！
            except Exception as e:
                print(f"❌ 请求模型锁定文件时发生异常: {str(e)}")
                print("⏭️ 跳过本轮排查...")
                continue

        # ----------------------------------------
        # 💥 增强版文件路径提取逻辑：防止大模型胡言乱语
        # ----------------------------------------
        suspect_file_path = ""
        for line in reversed(response_file.split('\n')):
            cleaned_line = line.replace('`', '').replace('*', '').strip()
            match = re.search(r'([a-zA-Z0-9_/\-\.]+\.(?:cpp|c|h|hpp))', cleaned_line)
            if match:
                suspect_file_path = match.group(1)
                break

        if not suspect_file_path:
            print(f"❌ 致命错误：无法从大模型的回复中提取到有效的文件路径！")
            print(f"大模型原始回复结尾如下：\n{response_file[-500:]}")
            print("⏭️ 强制跳过本轮排查...")
            continue

        analyzed_files_list.append(suspect_file_path)
        print(f"🎯 锁定可疑文件: {suspect_file_path}")

        # ----------------------------------------
        # 步骤 B：执行 AST 切割与 LLM 过滤
        # ----------------------------------------
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

        file_summary = "⚠️ 暂无全局功能摘要"
        if os.path.exists(json_summary_path):
            try:
                with open(json_summary_path, 'r', encoding='utf-8') as jf:
                    file_summary = json.load(jf).get("summary", file_summary)
            except Exception:
                pass

        functions_data_str = read_file_content(filtered_md_path)
        if not functions_data_str or "⚠️" in functions_data_str:
            functions_data_str = "该文件没有有效的函数切片数据或被全过滤。"

        # 🔥 异常保护 2：强行防爆截断 (防 OOM 强杀)
        max_char_limit = 120000
        if len(functions_data_str) > max_char_limit:
            print(f"⚠️ 警告：当前文件切片极大 ({len(functions_data_str)} 字符)，执行安全截断防护...")
            functions_data_str = functions_data_str[:max_char_limit] + "\n\n... [警告：因系统内存与上下文限制，超长后续代码已被安全截断] ..."

        prompt_analyze_functions = f"""
        【当前进度：第 {round_idx} 轮 / 共 20 轮 - 阶段：下钻函数分析】

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

        temp_messages = messages + [{"role": "user", "content": prompt_analyze_functions}]

        print(f"🔬 正在对文件内函数进行评估打分...")

        # 🔥 异常保护 3：函数打分异常隔离
        try:
            response = client.chat.completions.create(
                model="deepseek-reasoner",
                messages=temp_messages,
                max_tokens=40000
            )
            message_obj = response.choices[0].message
            response_funcs = message_obj.content or ""

            # 从推理内容兜底
            reasoning = getattr(message_obj, 'reasoning_content', "")
            if not response_funcs and reasoning:
                print("⚠️ 模型正式结论为空，尝试提取内部思维链...")
                response_funcs = reasoning

            if not response_funcs:
                raise ValueError("API 返回内容完全为空")

        except Exception as e:
            print(f"❌ 对函数进行打分时发生 API 崩溃或解析异常: {str(e)}")
            print("⏭️ 放弃当前文件的打分，安全跳入下一轮...")
            continue  # 跳过当前这轮文件的提取保存

        # ----------------------------------------
        # 步骤 D：解析提取表格并赋予 File_Rank，且单独保存单文件表格
        # ----------------------------------------
        round_funcs = extract_table_from_llm_response(response_funcs)

        # 单文件函数排名结果单独保存
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

    print(f"🎉 全部分析排查完成！终极全局排行已保存至: {final_ranked_md_path}")