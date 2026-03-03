import os
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


def root_cause(bugid, configFile, rightOption, wrongOption):
    cfg = ConfigParser()
    cfg.read(configFile)

    # 1. 读取配置文件路径
    baseLogDir = cfg.get('llvm-locations', 'logDir')
    baseCaseDir = cfg.get('llvm-locations', 'passdir')
    baseInfoPath = cfg.get('llvm-locations', 'infodir')

    # 获取 LLM API 配置 (请确保 config.ini 中有此节点)
    api_key = cfg.get('deepseek', 'api')
    api_url = cfg.get('deepseek', 'url')

    # 2. 构建目标文件路径
    logPath = os.path.join(baseLogDir, bugid, 'compile.log')
    passCaseDir = os.path.join(baseCaseDir, bugid, 'passing_cases')
    failCaseDir = os.path.join(baseCaseDir, bugid, 'failing_cases')
    ori = os.path.join(baseInfoPath, bugid, 'fail.c')

    print(f"🔍 正在加载 BugID: {bugid} 的文件与编译日志...")

    # 3. 加载内容
    ori_content = read_file_content(ori)
    log_content = read_file_content(logPath)
    fail_cases_str = load_mutated_cases(failCaseDir)
    pass_cases_str = load_mutated_cases(passCaseDir)

    if not ori_content or not log_content:
        print("❌ 致命错误：找不到原始 fail.c 或 compile.log 文件，中止分析。")
        return

    # 4. 组装 System Prompt 和 User Prompt
    system_prompt = (
        "你是一位顶尖的 LLVM 编译器开发专家与程序分析研究员，精通 C 语言规范、"
        "未定义行为（UB）分析、编译器优化管线、中间代码（IR）转换以及 x86-64 汇编。"
    )

    user_prompt = f"""任务目标
    我遇到了一起由于编译器优化等级导致的bug，编译器在{rightOption}不触发bug，在{wrongOption}触发bug。我将提供触发该 Bug 的原始代码、基于它变异的成功/失败测试用例集，以及详尽的编译阶段对比日志。请你执行深度差分分析，推导出导致该 Bug 的根因假设，并为后续锁定编译器源码中的具体缺陷位置提供指导。
    输入数据
    1. 原始触发 Bug 的测试程序 (fail.c)
    {ori_content}
    2. 变异测试用例集 (差分数据)
    保持 Bug 触发的变异程序 (Failing Cases - 说明这些变异没有破坏触发条件):
    {fail_cases_str}
    不再触发 Bug 的变异程序 (Passing Cases - 说明这些变异破坏了触发条件，是定位根因的钥匙):
    {pass_cases_str}
    3. 深度编译分析日志 ({rightOption} vs {wrongOption})
    {log_content}
    分析指令（请严格按以下步骤进行思维链推理）
    第一步：差分语法与语义分析 (Test Case Delta Analysis)
    对比原始程序、Failing Cases 和 Passing Cases。
    提取出导致程序从“触发 Bug”变成“不触发 Bug”的最小关键语法或语义突变（例如：某个循环边界的改变、指针别名的解除、变量作用域的改变等）。
    结合 Sanitizer 日志，排除由于测试用例本身存在未定义行为（UB）而导致触发bug的优化级别造成的结果不可预期的可能性。
    第二步：汇编与编译日志映射 (Log-to-Code Mapping)
    分析日志中不同优化等级的核心指令差异。
    利用日志中的交织反汇编（Interleaved ASM），将第一步中找到的“关键语法突变”直接映射到被错误优化掉或生成错误的底层汇编指令上。
    第三步：编译器内部状态推导 (IR & Optimization Pass Hypothesis)
    结合代码语义和汇编丢失的特征，推断是哪一类数据流分析或控制流分析得出了错误结论。
    提出 2-3 个最有可能的嫌疑优化 Pass，并详细解释该 Pass 是如何错误理解了 C 代码的语义的。
    输出格式要求
    请以结构化的技术报告形式输出你的分析结果：
    1. 触发条件隔离
    精确描述必须同时满足哪些代码特征（如特定的指针赋值+特定的循环嵌套）才能骗过优化器触发该 Bug。
    2. 根因假设分析
    详细解释编译器在理解这段代码时发生了什么逻辑断层。是依赖关系图建错了，还是别名分析过于激进？请给出清晰的推导过程。
    3. 高度嫌疑的优化 Pass 
    列出最有嫌疑的 LLVM Pass 名称，并给出对应的理由。"""
    print("🧠 提示词组装完毕，正在呼叫大模型进行深度推理 (请耐心等待)...")
    try:
        client = OpenAI(api_key=api_key, base_url=api_url, timeout=3600)
        response = client.chat.completions.create(
            model="deepseek-reasoner",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.2,
            max_tokens=50000
        )

        analysis_report = response.choices[0].message.content
        report_path = os.path.join(baseLogDir, bugid, f"root_cause.md")

        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(analysis_report)

        print(f"✅ 成功！根因分析报告已保存至: {report_path}")
    except Exception as e:
        print(f"❌ 调用大模型 API 失败: {str(e)}")