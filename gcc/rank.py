import csv
import math
import os
import sys
from configparser import ConfigParser
from collections import defaultdict


def fileRank(bugId, configFile):
    cfg = ConfigParser()
    cfg.read(configFile)
    infodir = cfg.get('gcc-locations', 'infodir')
    passdir = cfg.get('gcc-locations', 'passdir')
    logDir = cfg.get('gcc-locations', 'logDir')

    if not os.path.exists(logDir + bugId):
        os.makedirs(logDir + bugId, exist_ok=True)
    resultFile = logDir + bugId + '/SBFL.csv'
    result = open(resultFile, 'w')

    # 1. 确定失败用例的覆盖率文件路径
    if os.path.exists(infodir + bugId + '/failcov/stmt_info.txt'):
        tarpath = infodir + bugId + '/failcov/stmt_info.txt'
    elif os.path.exists(infodir + bugId + '/fail/stmt_info.txt'):
        tarpath = infodir + bugId + '/fail/stmt_info.txt'
    else:
        print(f"Error!! 找不到Bug {bugId} 的失败用例文件")
        sys.exit(1)

    with open(tarpath) as failfile:
        faillines = failfile.readlines()

    # 初始化数据结构
    failstmt = dict()
    passstmt = dict()
    failfileset = set()
    failfilemapstmt = defaultdict(set)

    # 2. 处理失败用例覆盖率数据
    for line in faillines:
        line = line.strip()
        if not line or ':' not in line:
            continue

        filepath, stmts = line.split(':', 1)
        # 【核心修复】：只取纯文件名，抛弃前面所有的文件夹前缀，确保能绝对匹配
        filename = os.path.basename(filepath.split('.gcda')[0])

        failfileset.add(filename)
        stmtlist = stmts.split(',')
        failfilemapstmt[filename].update(stmtlist)

        for stmt in stmtlist:
            if stmt:
                key = f"{filename},{stmt}"
                failstmt[key] = 1
                passstmt[key] = 0

    # 3. 处理通过用例的覆盖率数据
    passcov_path = os.path.join(passdir, bugId, 'passcov')
    if os.path.exists(passcov_path):
        for i in os.listdir(passcov_path):
            passfile_path = os.path.join(passcov_path, i, 'stmt_info.txt')
            if not os.path.exists(passfile_path):
                continue

            with open(passfile_path) as passfile:
                for line in passfile:
                    line = line.strip()
                    if not line or ':' not in line:
                        continue

                    filepath, stmts = line.split(':', 1)
                    # 【核心修复】：同步使用 basename 去匹配
                    filename = os.path.basename(filepath.split('.gcda')[0])

                    if filename not in failfileset:
                        continue

                    stmtlist = stmts.split(',')
                    # 只交集运算失败用例也覆盖了的语句
                    common_stmts = set(stmtlist) & failfilemapstmt[filename]

                    for stmt in common_stmts:
                        if stmt:
                            passstmt[f"{filename},{stmt}"] += 1

    # 4. 计算每个语句的可疑度得分
    score = dict()
    filescore = defaultdict(list)

    for key in failstmt.keys():
        pass_count = passstmt.get(key, 0)
        fail_count = failstmt[key]  # 这里是 1

        denom = math.sqrt(fail_count * (fail_count + pass_count))
        score_val = float(fail_count) / denom if denom != 0 else 0.0

        filename = key.split(',')[0]
        filescore[filename].append(score_val)

    # 5. 计算文件平均分并排序
    fileaggstmtscore = dict()
    for filename, scores in filescore.items():
        fileaggstmtscore[filename] = sum(scores) / len(scores)

    scorelist = sorted(fileaggstmtscore.items(), key=lambda d: d[1], reverse=True)

    # 6. 写入结果
    result.write("Rank,File,Score\n")
    for rank, (filename, score_val) in enumerate(scorelist, 1):
        result.write(f"{rank},{filename}.c,{score_val:.6f}\n")

    result.flush()
    result.close()