import csv
import glob
import json
import os.path
import re
import shutil
from configparser import ConfigParser

from openai import OpenAI


def read_file_content(filepath):
    if filepath and os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read().strip()
                return content if content else "Empty file."
        except Exception as e:
            return f"Error reading file: {e}"
    return "File not found."


def read_all_candidates(directory, prefix, extension):
    pattern = os.path.join(directory, f"{prefix}*{extension}")
    files = glob.glob(pattern)

    if not files:
        return "No files found."

    # 定义排序键：提取文件名中的数字进行数值排序 (1, 2, ... 10)
    def extract_number(filepath):
        match = re.search(r'(\d+)', os.path.basename(filepath))
        return int(match.group(1)) if match else 0

    # 按文件名中的数字排序
    sorted_files = sorted(files, key=extract_number)

    all_content = []
    for filepath in sorted_files:
        filename = os.path.basename(filepath)
        content = read_file_content(filepath)
        # 格式化单个文件内容
        file_block = (
            f"--- File: {filename} ---\n"
            f"{content}\n"
        )
        all_content.append(file_block)

    return "\n".join(all_content)


def bug_reasoning(configPath, bugId):
    config = ConfigParser()
    config.read(configPath)
    baseReasonDir = config.get('llvm-locations', 'reasonDir')
    summaryDir = config.get('llvm-locations', 'summaryDir')
    api = config.get('deepseek', 'api')
    url = config.get('deepseek', 'url')
    client = OpenAI(
        api_key=api,
        base_url=url
    )
    reasonDir = os.path.join(baseReasonDir, bugId)
    fileList = abstract_top_ten_file(configPath, bugId)

    context_data = []

    for item in fileList:
        rank = item['rank']
        full_file_path = item['file']
        file_name_only = os.path.basename(full_file_path)
        json_filename = f"{file_name_only}.json"
        json_path = os.path.join(summaryDir, json_filename)
        summary_text = "No summary available."
        if os.path.exists(json_path):
            try:
                with open(json_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    summary_text = data.get('summary', summary_text)
            except Exception as e:
                print(f"Error reading JSON for {file_name_only}: {e}")
        else:
            print(f"Warning: JSON file not found: {json_path}")

        entry = (
            f"Rank: {rank}\n"
            f"File: {full_file_path}\n"
            f"Summary: {summary_text}\n"
            f"----------------------------------------"
        )
        context_data.append(entry)
    suspicious_file_context = "\n".join(context_data)

    log_path = os.path.join(reasonDir, 'compiler-log.txt')
    log_content = read_file_content(log_path)
    buggy_path = os.path.join(reasonDir, 'fail.c.txt')
    buggy_content = read_file_content(buggy_path)
    failing_content_all = read_all_candidates(reasonDir, 'still-fail-', '.c.txt')
    success_content_all = read_all_candidates(reasonDir, 'pass-', '.c.txt')
    system_prompt = (
            "role:你是一位资深 LLVM 编译器工程师。你精通编译器架构，特别是 LLVM IR（中间表示）、"
            "优化 Pass、指令选择以及寄存器分配机制。\n"
            "goal:你的任务是根据提供的多源信息进行根本原因分析，核心线索在于对比“触发 Bug 的变异程序”"
            "与“不触发 Bug 的变异程序”，推导出导致 LLVM 产生bug的原因。"
    )
    user_prompt = f"""
    输入材料
    [Compilation Log]: 编译器编译触发bug的测试程序的日志。
    {log_content}

    [Bug-Triggering Program]: 最初触发 Bug 的测试代码。
    {buggy_content}

    [Still-Failing Program]: 基于原程序变异后，依然触发 Bug 的精简代码。
    {failing_content_all}

    [Success Program]: 基于原程序变异后，不再触发 Bug 的代码（通常与前者只有微小差异）。
    {success_content_all}

    [Suspicious File]: 已经筛选出的最有可能的Buggy文件Top-10和文件功能摘要
    {suspicious_file_context}

    分析步骤：
    首先分析 [Compilation Log]。指出编译器是在哪个阶段（Pass）崩溃的？
    然后请逐行对比[Still-Failing Program]和[Success Program]，找出它们的核心差异，明确指出：为什么这个微小的差异导致了编译器行为的分歧？
    结合前两个步骤与[Suspicious File]，构建因果链，解释为什么[Success Program]能够绕过这个错误
    最后总结bug发生的原因，并在markdown代码框中给出，便于提取。
    """

    try:
        print(f"Analyzing Bug {bugId}...")
        response = client.chat.completions.create(
            model="deepseek-reasoner",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.1,
            max_tokens=4096
        )
        result = response.choices[0].message.content
        output_path = os.path.join(reasonDir, 'bug_analysis_report.md')
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(result)
        return result
    except Exception as e:
        print(f"API Call Failed: {e}")
        return None


def abstract_top_ten_file(configPath, bugId):
    config = ConfigParser()
    config.read(configPath)
    baseFileListdir = config.get('llvm-locations', 'fileListdir')
    fileList = os.path.join(baseFileListdir, bugId, 'aggregate_result.csv')
    suspicious_files = []
    if not os.path.exists(fileList):
        print(f"Error: File not found: {fileList}")
        return []
    try:
        with open(fileList, mode='r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for index, row in enumerate(reader):
                if index >= 10:
                    break
                file_info = {
                    'rank': row['Rank'],
                    'file': row['File'],
                }
                suspicious_files.append(file_info)
    except Exception as e:
        print(f"Error reading CSV file: {e}")
        return []
    return suspicious_files


def abstract_method(configPath, bugId, version):
    fileList = abstract_top_ten_file(configPath, bugId)
    config = ConfigParser()
    config.read(configPath)
    baseCompilerDir = config.get('llvm-locations', 'compilerDir')
    baseReasonDir = config.get('llvm-locations', 'reasonDir')
    compilerDir = os.path.join(baseCompilerDir, version, version, 'llvm')
    reasonDir = os.path.join(baseReasonDir, bugId)
    api = config.get('deepseek', 'api')
    url = config.get('deepseek', 'url')
    client = OpenAI(
        api_key=api,
        base_url=url
    )
    for item in fileList:
        relative_path = item['file']
        src_path = os.path.join(compilerDir, relative_path)
        file_name = os.path.basename(relative_path)
        dst_path = os.path.join(reasonDir, file_name)

        # 1. 复制文件
        if os.path.exists(src_path):
            try:
                shutil.copy2(src_path, dst_path)
                print(f"Successfully copied: {file_name}")

                # 2. 读取刚复制的 C++ 代码
                with open(dst_path, 'r', encoding='utf-8', errors='ignore') as f:
                    cpp_content = f.read()

                # 如果文件太小，可能不是有效代码，跳过
                if len(cpp_content) < 10:
                    print(f"Skipping {file_name}: Content too short.")
                    continue

                # 3. 构造 Prompt 提取函数
                print(f"Extracting functions from {file_name} via AI...")

                system_prompt = "你是一位专业的 C++ 代码解析专家。你的任务是从所提供的源代码中提取出所有可执行的函数。"

                user_prompt = f"""
                    请分析以下 C++ 源代码文件：{file_name}。
                    提取其中所有的可执行函数（包括成员函数和普通函数）。
                    请忽略只有声明没有实现的函数。
                
                    输出必须是严格的 JSON 格式，不要包含任何 Markdown 标记（如 ```json）。
                    JSON 结构如下：
                    {{
                      "file_name": "{file_name}",
                      "functions": [
                        {{
                          "name": "函数名称(包含类名，如 LoopVectorize::run)",
                          "summary": "用中文简要描述该函数的功能"
                        }}
                      ]
                    }}
                
                    源代码内容：
                    {cpp_content}
                    """
                try:
                    response = client.chat.completions.create(
                        model="deepseek-chat",
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt}
                        ],
                        temperature=0.1,
                    )

                    result_text = response.choices[0].message.content

                    cleaned_json_str = re.sub(r'^```json\s*', '', result_text, flags=re.MULTILINE)
                    cleaned_json_str = re.sub(r'\s*```$', '', cleaned_json_str, flags=re.MULTILINE)

                    json_data = json.loads(cleaned_json_str)
                    json_output_path = os.path.join(reasonDir, f"{file_name}.json")

                    with open(json_output_path, 'w', encoding='utf-8') as jf:
                        json.dump(json_data, jf, indent=2, ensure_ascii=False)

                    print(f"Saved function extraction to: {json_output_path}")

                except json.JSONDecodeError:
                    print(f"Error: Failed to parse JSON response for {file_name}")
                    with open(os.path.join(reasonDir, f"{file_name}_error.txt"), 'w') as ef:
                        ef.write(result_text)
                except Exception as e:
                    print(f"AI Processing Error for {file_name}: {e}")

            except Exception as e:
                print(f"Error copying/processing {file_name}: {e}")
        else:
            print(f"Warning: Source file not found: {src_path}")


# bug_reasoning('../config/config.ini', '15920')
abstract_method('../config/config.ini', '15920', 'r181189')