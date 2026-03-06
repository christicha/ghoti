import os
import re
import csv
import json
from configparser import ConfigParser
from openai import OpenAI


def function_FL(bugid, configPath):
    cfg = ConfigParser()
    cfg.read(configPath)

    # 1. 获取路径配置
    baseLogDir = cfg.get('llvm-locations', 'logDir')
    baseResultDir = cfg.get('llvm-locations', 'resultDir')
    baseFileListDir = cfg.get('llvm-locations', 'fileListDir')
    summaryDir = cfg.get('llvm-locations', 'summaryDir')
    baseInfoDir = cfg.get('llvm-locations', 'infodir')

    logDir = os.path.join(baseLogDir, bugid)
    resultDir = os.path.join(baseResultDir, bugid)
    fileListDir = os.path.join(baseFileListDir, bugid)

    # 获取 LLM API 配置
    api_key = cfg.get('deepseek', 'api')
    api_url = cfg.get('deepseek', 'url')

    ori_fail = os.path.join(baseInfoDir, bugid, 'fail.c')
    if not os.path.exists(ori_fail):
        ori_fail = os.path.join(logDir, 'fail.c')

    compile_log = os.path.join(logDir, 'compile.log')
    # file_rank_path = os.path.join(resultDir, 'final_file_rank.csv')
    file_rank_path = os.path.join('/home/chris/FLL-workplace/llvmbugs/result', bugid, 'aggregate_result.csv')

    print(f"\n==============================================")
    print(f"🎯 开始为 BugID: {bugid} 执行多轮单文件函数级审讯")
    print(f"==============================================\n")

    # 2. 读取全局上下文
    fail_code = "⚠️ 测试用例代码不存在。"
    if os.path.exists(ori_fail):
        with open(ori_fail, 'r', encoding='utf-8') as f:
            fail_code = f.read()

    compile_diff = "⚠️ 编译日志不存在。"
    if os.path.exists(compile_log):
        with open(compile_log, 'r', encoding='utf-8') as f:
            compile_diff = f.read()

    # 3. 解析 Top 10 文件名单
    top_10_files = []
    if os.path.exists(file_rank_path):
        with open(file_rank_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                if i >= 10: break
                top_10_files.append({
                    "rank": i + 1,
                    "file": row.get('File', '').strip()
                })
    else:
        print(f"❌ 找不到文件排名结果: {file_rank_path}")
        return

    client = OpenAI(api_key=api_key, base_url=api_url, timeout=3600)

    # 用于存储所有提取出来的表格行
    all_suspicious_functions = []

    # 4. 循环十轮进行审讯
    for file_info in top_10_files:
        rank_num = file_info['rank']
        full_filepath = file_info['file']
        basename = os.path.basename(full_filepath)

        print(f"🔍 [Round {rank_num}/10] 正在审问文件: {basename} ...")

        # 获取该文件的功能摘要
        file_summary = "⚠️ 未获取到文件摘要"
        json_path = os.path.join(summaryDir, f"{basename}.json")
        if os.path.exists(json_path):
            try:
                with open(json_path, 'r', encoding='utf-8') as jf:
                    data = json.load(jf)
                    file_summary = data.get("summary", "无 summary 字段")
            except Exception:
                pass

        # 获取嫌疑函数库
        filtered_md_path = os.path.join(fileListDir, f"{basename}_filtered.md")
        candidate_funcs = "⚠️ 该文件没有被过滤后的候选函数库或全被剔除。"
        if os.path.exists(filtered_md_path):
            with open(filtered_md_path, 'r', encoding='utf-8') as f:
                candidate_funcs = f.read().strip()

        # 如果该文件没有任何执行过的函数，直接跳过
        if not candidate_funcs or "未被覆盖率命中" in candidate_funcs:
            print(f"   ⏭️ 跳过：该文件没有有效的动态执行函数。")
            continue

        # 5. 组装单轮 Prompt
        system_prompt = (
            "你是一位拥有十余年经验的顶尖 LLVM 编译器底层研发专家与静态分析专家。"
            "你精通编译器优化管线、中间代码（IR）转换、以及底层机器码生成逻辑，"
            "极其擅长从海量源码中敏锐地捕捉引发编译器崩溃或错误优化的逻辑缺陷。"
        )

        user_prompt = f"""# 任务目标
        我正在进行编译器缺陷定位，我已经将引发某个编译器 Bug 的嫌疑范围锁定到了一个特定的高危源文件内。
        现在，我将为你提供该 Bug 的全局表现特征，以及该高危文件内切实被失败测试用例执行过的候选函数列表。请你对这些候选函数进行交叉审查，揪出最有可能藏匿该 Bug 的具体函数。
        
        # 输入数据
        ## 1. 触发 Bug 的测试程序
        【说明：这是导致编译器出现异常优化的原始 C/C++ 代码。】
        
        {fail_code}
        
        ## 2. 编译异常特征
        
        【说明：这是该测试用例在编译时暴露出的完整编译日志及异常特征】
        {compile_diff}
        
        ## 3. 高危文件画像
        
        * 文件所在全局可疑度排名: Top-{rank_num}
        * 文件核心功能摘要: {file_summary}
        
        ## 4. 嫌疑函数库
        
        【说明：以下是该文件内部，在 Bug 触发期间被真实调用过的核心业务函数及其功能摘要。】
        {candidate_funcs}
        
        # 分析指令（请严格按以下思维链执行推理）
        
        第一步：Bug 症状解码
        首先阅读【测试程序】与【编译异常特征】。用一到两句话精准概括：这个 Bug 到底是在处理什么语法结构、什么数据类型或什么 IR 指令时翻了车？。
        第二步：文件环境匹配
        结合【高危文件画像】，思考该文件所处的编译管线阶段（如前端 AST 解析、中端 Loop 优化、后端寄存器分配）是否恰好是引发上述症状的“第一案发现场”？这个极高的全局排名是否合理？
        第三步：函数逐个“审问”
        带入第一步提炼出的“作案特征”，逐一审视【嫌疑函数库】中的每一个函数。
        
        * 剔除无辜者：如果某函数只负责毫不相干的基础校验或内存清理，请果断排除。
        * 锁定嫌疑人：如果某函数的【功能摘要】完美契合了 Bug 的触发特征（例如：它正好负责处理那个出问题的节点或优化 Pass），请将其标记为高危。注意：务必警惕嵌套调用。
        
        # 输出格式要求
        
        请挑选出你认为最可疑的函数（多选，按可疑程度降序排列），并使用 Markdown 表格输出：
        | 危险度排名 | 函数签名 (Signature) | 嫌疑指数 (1-10) |
        """

        try:
            response = client.chat.completions.create(
                model="deepseek-reasoner",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3,
                max_tokens=8192
            )

            reply_text = response.choices[0].message.content

            # 6. 正则提取生成的 Markdown 表格数据
            table_match = re.search(r'\|.*?(危险度|Rank|签名|Signature|指数).*?\|.*?(?=\n\n|\Z)', reply_text,
                                    re.IGNORECASE | re.DOTALL)

            if table_match:
                table_lines = table_match.group(0).strip().split('\n')
                for line in table_lines:
                    line = line.strip()
                    if '危险度' in line or 'Rank' in line or '---' in line:
                        continue
                    if line.startswith('|') and line.endswith('|'):
                        parts = [p.strip() for p in line.split('|')]
                        if len(parts) >= 4:
                            clean_rank = parts[1]
                            clean_sig = parts[2]
                            clean_score = parts[3]
                            merged_line = f"| {clean_rank} | `{basename}` | {clean_sig} | {clean_score} |"
                            all_suspicious_functions.append(merged_line)

            print(f"   ✅ 完成分析，已提取相关嫌疑函数。")

        except Exception as e:
            print(f"   ❌ API 调用失败: {str(e)}")

    print("\n📊 正在汇总所有 10 轮审讯结果...")
    final_report_path = os.path.join(resultDir, 'final_function_rank.md')

    with open(final_report_path, 'w', encoding='utf-8') as f:
        f.write("| 原单文件内危险度 | 来源文件 (Source File) | 函数签名 (Signature) | 嫌疑指数 (1-10) |\n")
        f.write("| :--- | :--- | :--- | :--- |\n")
        if all_suspicious_functions:
            f.write("\n".join(all_suspicious_functions))
        else:
            f.write("| - | - | ⚠️ 未提取到任何符合要求的函数表项 | - |\n")

    print(f"🎉 汇总榜单已保存至: {final_report_path}")


def compute_comprehensive_rank(bugid, configPath):
    cfg = ConfigParser()
    cfg.read(configPath)

    resultDir = os.path.join(cfg.get('llvm-locations', 'resultDir'), bugid)
    md_path = os.path.join(resultDir, 'final_function_rank.md')

    if not os.path.exists(md_path):
        print(f"❌ 找不到需要重新排序的文件: {md_path}")
        return

    print(f"\n==============================================")
    print(f"🧮 开始计算全局函数的综合排名 (指数衰减加权模型)")
    print(f"==============================================\n")

    # 1. 提取所有函数行
    with open(md_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    functions_data = []
    # 用于记录文件名的出现顺序，推断其对应的全局文件排名 (因为前面的输出是按 Top1 到 Top10 顺序写入的)
    file_rank_map = {}
    current_file_rank = 1

    for line in lines:
        line = line.strip()
        if not line.startswith('|') or '危险度' in line or '---' in line or '未提取到' in line:
            continue

        parts = [p.strip() for p in line.split('|')]
        if len(parts) >= 5:
            # Markdown表格格式: | 原排名 | `文件名` | 签名 | 指数 |
            filename = parts[2].replace('`', '')
            signature = parts[3]
            try:
                # 提取 LLM 给出的嫌疑指数 (1-10)
                llm_score_match = re.search(r'\d+', parts[4])
                llm_score = float(llm_score_match.group()) if llm_score_match else 0.0
            except ValueError:
                llm_score = 0.0

            # 动态构建文件的全局排名
            if filename not in file_rank_map:
                file_rank_map[filename] = current_file_rank
                current_file_rank += 1

            functions_data.append({
                'filename': filename,
                'signature': signature,
                'llm_score': llm_score,
                'file_rank': file_rank_map[filename]
            })

    # 2. 计算综合得分 (指数衰减策略)
    # 公式：FinalScore = LLM_Score * (gamma ^ (File_Rank - 1))
    gamma = 0.85

    for item in functions_data:
        file_rank = item['file_rank']
        llm_score = item['llm_score']

        # 核心加权计算
        item['final_score'] = llm_score * (gamma ** (file_rank - 1))

    # 3. 按照综合得分进行全局降序排序
    functions_data.sort(key=lambda x: x['final_score'], reverse=True)

    # 4. 生成终极重排版 Markdown
    final_ranked_md_path = os.path.join(resultDir, 'ultimate_function_rank.md')

    with open(final_ranked_md_path, 'w', encoding='utf-8') as f:
        f.write("# 🏆 [终极] 跨文件函数综合可疑度全局排位表\n\n")
        f.write("| 全局绝对排名 | 来源文件 (File Rank) | 函数签名 (Signature) | 综合得分 (Max=10) | LLM原始打分 |\n")
        f.write("| :---: | :--- | :--- | :---: | :---: |\n")

        for i, item in enumerate(functions_data):
            f.write(f"| 🥇 **Top-{i + 1}** " if i < 3 else f"| Top-{i + 1} ")
            f.write(f"| `{item['filename']}` (Rank: {item['file_rank']}) ")
            f.write(f"| `{item['signature']}` ")
            f.write(f"| **{item['final_score']:.2f}** ")
            f.write(f"| {item['llm_score']} |\n")

    print(f"🎉 综合排名计算完毕！终极榜单已保存至: {final_ranked_md_path}")