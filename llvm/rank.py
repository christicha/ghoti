import csv
import math
import os
import sys
from configparser import ConfigParser
import re
from collections import defaultdict
from itertools import islice

def deleteGcdaPath(gcdaDirName):
    return re.sub(r'/CMakeFiles/[^/]+.dir', '', gcdaDirName)


def fileRank(bugId, configFile):
    cfg = ConfigParser()
    cfg.read(configFile)
    infodir = cfg.get('llvm-locations', 'infodir')
    passdir = cfg.get('llvm-locations', 'passdir')
    logDir = cfg.get('llvm-locations', 'logDir')
    resultFile = logDir + bugId + '/SBFL.csv'
    result = open(resultFile, 'w')
    # 读取bug位置信息文件
    locationfile = open(infodir + bugId + '/locations')
    locationlines = locationfile.readlines()
    locationfile.close()
    buggyfiles = set()  # 存储包含bug的文件集合

    # 解析位置文件，提取bug所在文件
    for i in range(len(locationlines)):
        if 'file' in locationlines[i].strip() and 'method' in locationlines[i].strip():
            # 提取文件路径，格式化为标准格式
            buggyfile = 'lib/' + \
                        locationlines[i].strip().split(';')[0].strip().split(':')[1].strip().split('/lib/')[1]
            buggyfiles.add(buggyfile)  # 添加到bug文件集合

    # 确定失败用例的覆盖率文件路径
    if os.path.exists(infodir + bugId + '/failcov/stmt_info.txt'):
        tarpath = infodir + bugId + '/failcov/stmt_info.txt'
    elif os.path.exists(infodir + bugId + '/fail/stmt_info.txt'):
        tarpath = infodir + bugId + '/fail/stmt_info.txt'
    else:
        print("Error!!")
        sys.exit(1)

    # 读取失败用例的语句覆盖率信息
    failfile = open(tarpath)
    faillines = failfile.readlines()
    failfile.close()

    # 初始化数据结构
    failstmt = dict()  # 记录每个语句被失败用例覆盖的次数
    passstmt = dict()  # 记录每个语句被通过用例覆盖的次数
    failfileset = set()  # 失败用例覆盖的文件集合
    failfilemapstmt = dict()  # 每个文件对应的语句集合映射

    # 处理fail.c的覆盖率数据
    for i in range(len(faillines)):
        faillinesplit = faillines[i].strip().split(',')
        filename = faillinesplit[0].strip().split('.gcda')[0].strip()  # 提取文件名
        filename = deleteGcdaPath(filename)
        if not filename.endswith('.cpp'):  # 只处理.cpp文件
            continue
        failfileset.add(filename)  # 添加到失败文件集合
        stmtlist = faillines[i].strip().split(':')[1].split(',')  # 提取语句列表
        failfilemapstmt[filename] = set(stmtlist)  # 建立文件到语句的映射

        # 初始化每个语句的计数
        for stmt in stmtlist:
            failstmt[filename + ',' + stmt] = 1  # 失败用例覆盖1次
            passstmt[filename + ',' + stmt] = 0  # 通过用例覆盖0次（初始）

    # 处理所有通过测试用例的覆盖率
    for i in os.listdir(passdir + bugId + '/passcov'):
        # 读取单个通过用例的覆盖率文件
        passfile = open(passdir + bugId + '/passcov/' + i + '/stmt_info.txt')
        passlines = passfile.readlines()
        passfile.close()

        # 处理通过用例的覆盖率数据
        for j in range(len(passlines)):
            passlinesplit = passlines[j].strip().split(',')
            filename = passlinesplit[0].strip().split('.gcda')[0].strip()
            filename = deleteGcdaPath(filename)
            if not filename.endswith('.cpp'):  # 只考虑.cpp文件
                continue
            if filename not in failfileset:  # 只处理失败用例也覆盖的文件
                continue
            stmtlist = passlines[j].strip().split(':')[1].split(',')
            # 统计通过用例覆盖的语句（只统计失败用例也覆盖的语句）
            for stmt in set(stmtlist) & failfilemapstmt[filename]:
                passstmt[filename + ',' + stmt] += 1  # 增加通过用例计数

    # 计算每个语句的可疑度得分
    score = dict()
    filescore = dict()

    for key in failstmt.keys():
        # 核心评分公式：基于失败和通过覆盖率的比值
        score[key] = float(failstmt[key]) / math.sqrt(float(failstmt[key]) * (failstmt[key] + passstmt[key]))
        keyfile = key.split(',')[0]  # 提取文件名
        # 按文件聚合语句得分
        if keyfile not in filescore.keys():
            filescore[keyfile] = []
            filescore[keyfile].append(score[key])
        else:
            filescore[keyfile].append(score[key])

    # 计算每个文件的平均可疑度得分
    fileaggstmtscore = dict()
    for key in filescore.keys():
        fileaggstmtscore[key] = float(sum(filescore[key])) / len(filescore[key])

    # 按得分降序排序文件
    scorelist = sorted(fileaggstmtscore.items(), key=lambda d: d[1], reverse=True)

    # 统计得分为1.0的文件数量（最可疑的文件）
    number_1po0 = 0
    for j in range(len(scorelist)):
        if scorelist[j][0] == 1.0:
            number_1po0 += 1
    # 写入完整的可疑度排名（所有文件）
    result.write("Rank,File,Score\n")  # CSV 表头
    for rank, (filename, score_val) in enumerate(scorelist, 1):
        result.write(f"{rank},{filename},{score_val:.6f}\n")

    # 添加详细的buggy文件信息
    # result.write(f"\n# Buggy Files Details\n")
    # for bf in buggyfiles:
    #     # 在完整排名中查找已知 buggy 文件的位置
    #     found_rank = None
    #     found_score = None
    #     for rank, (filename, score_val) in enumerate(scorelist, 1):
    #         setbf = set(bf.split('/'))
    #         seti = set(filename.split('/'))
    #         if setbf.issubset(seti):
    #             found_rank = rank
    #             found_score = score_val
    #             break
    #
    #     if found_rank is not None:
    #         result.write(f"Buggy file: {bf} -> Rank: {found_rank}, Score: {found_score:.6f}\n")
    #     else:
    #         result.write(f"Buggy file: {bf} -> Not found in ranking\n")
    result.flush()
    result.close()

