import os
import json
import re
from typing import Dict
from configparser import ConfigParser


def parse_file_coverage(filepath: str) -> Dict[str, int]:
    """解析单个 coverage 文件，返回 {文件名: 调用次数} 的字典"""
    filemap: Dict[str, int] = {}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            for line in lines:
                items = line.split(',')
                if len(items) < 2:  # 跳过格式异常的行
                    continue

                # 使用正则清洗文件名路径
                filename = re.sub(r'/CMakeFiles/[^/]+\.dir', '', items[0]).split('.gcda')[0]

                # 累加调用次数，并过滤掉执行次数为 0 的文件
                try:
                    count = int(items[-1].strip())
                    if count > 0:
                        filemap[filename] = filemap.get(filename, 0) + count
                except ValueError:
                    continue
    except Exception as e:
        print(f"❌ 读取覆盖率文件失败 {filepath}: {str(e)}")

    return filemap


def generate_abstract_summary(bugid, configPath):
    """
    基于所有失败测试用例的覆盖率【交集】，提取文件执行次数和功能摘要，并写入目标文件。
    """
    cfg = ConfigParser()
    cfg.read(configPath)

    # 1. 切换为读取 gcc-locations 的路径配置
    baseLogDir = cfg.get('gcc-locations', 'logDir')
    summaryDir = cfg.get('gcc-locations', 'summaryDir')
    infoBasePath = cfg.get('gcc-locations', 'infodir')
    baseFailDir = cfg.get('gcc-locations', 'passdir')

    resultPath = os.path.join(baseLogDir, bugid, 'abstract_summary.txt')
    methodcov = os.path.join(infoBasePath, bugid, 'failcov', 'method_info.txt')
    # 失败用例的变异目录
    failcov_dir = os.path.join(baseFailDir, bugid, 'failcov')

    # 2. 收集所有失败用例的 method_info.txt 路径
    all_method_info_paths = []
    if os.path.exists(methodcov):
        all_method_info_paths.append(methodcov)

    if os.path.exists(failcov_dir):
        for entry in os.listdir(failcov_dir):
            sub_dir_path = os.path.join(failcov_dir, entry)
            if os.path.isdir(sub_dir_path):
                sub_method_info = os.path.join(sub_dir_path, 'method_info.txt')
                if os.path.exists(sub_method_info):
                    all_method_info_paths.append(sub_method_info)

    if not all_method_info_paths:
        print(f"⚠️ 警告: 未找到任何失败用例的覆盖率文件。")
        return False

    # 3. 解析所有覆盖率文件
    all_filemaps = []
    for path in all_method_info_paths:
        fmap = parse_file_coverage(path)
        if fmap:  # 只有非空的字典才加入
            all_filemaps.append(fmap)

    if not all_filemaps:
        print(f"⚠️ 警告: 所有覆盖率文件均为空或读取失败。")
        return False

    # 4. 计算文件的【交集】
    original_file_count = len(all_filemaps[0])  # ✅ 记录原始 info 涉及的文件数
    common_files = set(all_filemaps[0].keys())

    for fmap in all_filemaps[1:]:
        common_files.intersection_update(fmap.keys())

    if not common_files:
        print(f"⚠️ 警告: 失败用例之间没有文件覆盖率交集 (原 info 涉及 {original_file_count} 个文件)。摘要文件将为空。")
        final_filemap = {}
    else:
        # ✅ 打印原始数量和交集后的数量，展示过滤效果
        filtered_count = original_file_count - len(common_files)
        print(
            f"✅ 覆盖率清洗完成！原 info 涉及 {original_file_count} 个文件，取交集后剩余 {len(common_files)} 个核心文件 (成功滤除 {filtered_count} 个噪音文件)。")

        # 将交集文件的调用次数进行累加求和
        final_filemap = {f: sum(fmap[f] for fmap in all_filemaps) for f in common_files}

    # 5. 根据交集 final_filemap 和 summaryDir 提取 JSON 摘要
    result_lines = []
    for filename, call_count in final_filemap.items():
        temp = os.path.basename(filename)

        # 优先级判断：寻找对应 JSON 文件时依然按照真实后缀找
        json_path_cc = os.path.join(summaryDir, f"{temp}.cc.json")
        json_path_c = os.path.join(summaryDir, f"{temp}.c.json")
        json_path_h = os.path.join(summaryDir, f"{temp}.h.json")

        json_path = None
        if os.path.exists(json_path_cc):
            json_path = json_path_cc
        elif os.path.exists(json_path_c):
            json_path = json_path_c
        elif os.path.exists(json_path_h):
            json_path = json_path_h
        else:
            # 如果对应 JSON 都不存在，直接跳过
            continue

        try:
            with open(json_path, 'r', encoding='utf-8') as json_file:
                data = json.load(json_file)
                summary = data.get('summary', '无摘要信息')
                # 写入结果时，强制把后缀统一定义为 .c
                result_lines.append(f"{temp}.c，总调用次数:{call_count}：{summary}")
        except json.JSONDecodeError:
            print(f"⚠️ 警告：{temp}.c，调用次数:{call_count}：JSON格式错误")
        except Exception as e:
            print(f"❌ 错误：{temp}.c，调用次数:{call_count}：读取错误 - {str(e)}")

    # 6. 尝试写入目标文件
    try:
        # 确保目标文件所在的目录结构存在
        os.makedirs(os.path.dirname(resultPath), exist_ok=True)

        with open(resultPath, 'w', encoding='utf-8') as out_file:
            if result_lines:
                out_file.write('\n'.join(result_lines))
            else:
                out_file.write("无满足条件的文件摘要。")

        print(f"✅ 摘要信息已成功生成并写入: {resultPath}")
        return True
    except Exception as e:
        print(f"❌ 写入摘要文件失败 {resultPath}: {str(e)}")
        return False

# if __name__ == "__main__":
#     generate_abstract_summary('15920', '/home/chris/ghoti/config/config.ini')