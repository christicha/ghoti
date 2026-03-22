import os
import json
import re
from typing import Dict
from configparser import ConfigParser


def generate_abstract_summary(bugid, configPath):
    """
    根据执行次数和文件功能摘要文档，提取摘要信息并写入目标文件。
    """
    cfg = ConfigParser()
    cfg.read(configPath)
    # 1. 加载路径配置
    baseLogDir = cfg.get('llvm-locations', 'logDir')
    summaryDir = cfg.get('llvm-locations', 'summaryDir')
    infoBasePath = cfg.get('llvm-locations', 'infodir')

    resultPath = os.path.join(baseLogDir, bugid, 'abstract_summary.txt')
    methodcov = os.path.join(infoBasePath, bugid, 'failcov', 'method_info.txt')

    filemap: Dict[str, int] = {}

    # 2. 读取 coverage 文件并统计调用次数
    if os.path.exists(methodcov):
        try:
            with open(methodcov, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                for line in lines:
                    items = line.split(',')
                    if len(items) < 2:  # 跳过格式异常的行
                        continue

                    # 使用正则清洗文件名路径
                    filename = re.sub(r'/CMakeFiles/[^/]+\.dir', '', items[0]).split('.gcda')[0]

                    # 累加调用次数
                    try:
                        count = int(items[-1].strip())
                        filemap[filename] = filemap.get(filename, 0) + count
                    except ValueError:
                        continue  # 跳过无法转换为整数的行
        except Exception as e:
            print(f"❌ 读取覆盖率文件失败 {methodcov}: {str(e)}")
            return False
    else:
        print(f"⚠️ 警告: 覆盖率文件不存在 {methodcov}")
        return False

    # 3. 根据 filemap 和 summaryDir 提取摘要
    result_lines = []
    for filename, call_count in filemap.items():
        temp = os.path.basename(filename)
        json_path = os.path.join(summaryDir, f"{temp}.json")

        if os.path.exists(json_path):
            try:
                with open(json_path, 'r', encoding='utf-8') as json_file:
                    data = json.load(json_file)
                    summary = data.get('summary', '无摘要信息')
                    result_lines.append(f"{filename}，调用次数:{call_count}：{summary}")
            except json.JSONDecodeError:
                print(f"⚠️ 警告：{filename}，调用次数:{call_count}：JSON格式错误")
            except Exception as e:
                print(f"❌ 错误：{filename}，调用次数:{call_count}：读取错误 - {str(e)}")
        else:
            # 如果对应 JSON 不存在，直接跳过
            continue

    # 4. 尝试写入目标文件
    try:
        # 确保目标文件所在的目录结构存在
        os.makedirs(os.path.dirname(resultPath), exist_ok=True)

        with open(resultPath, 'w', encoding='utf-8') as out_file:
            out_file.write('\n'.join(result_lines))

        print(f"✅ 摘要信息已成功生成并写入: {resultPath}")
        return True
    except Exception as e:
        print(f"❌ 写入摘要文件失败 {resultPath}: {str(e)}")
        return False


if __name__ == "__main__":
    generate_abstract_summary('15920', '/home/chris/ghoti/config/config.ini')
