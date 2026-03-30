from configparser import ConfigParser
from llvm.abstract import generate_abstract_summary
from llvm.functionFL_3 import function_FL
from llvm.generateMutate import generateMutate
from llvm.log import batch_generate_logs
from llvm.rank import fileRank

config = ConfigParser()
config.read('./config/config.ini', encoding='utf-8')
compilerBasePath = config.get('llvm-locations', 'compilersdir')
baseResultPath = config.get('llvm-locations', 'resultDir')
buglist = config.get('llvm-locations', 'bugList')
passBasePath = config.get('llvm-locations', 'passdir')
configPath = config.get('llvm-locations', 'configFile')

bugIds = []
revisions = []
compileOptionRights = []
compileOptionWrongs = []
checks = []

with open(buglist, 'r') as f:
    for line in f:
        line = line.strip()
        items = line.split(',')
        bugIds.append(items[0])
        revisions.append(items[1])
        compileOptionRights.append(items[2])
        compileOptionWrongs.append(items[3])
        checks.append(items[4])

from concurrent.futures import ProcessPoolExecutor, as_completed


def process_bugid_wrapper(args):
    i, bugIds, revisions, compileOptionRights, compileOptionWrongs, checks, configPath, passBasePath = args
    bugid = bugIds[i]
    revision = revisions[i]
    compileOptionRight = compileOptionRights[i]
    compileOptionWrong = compileOptionWrongs[i]
    check = checks[i]
    # generateMutate(bugid, revision, check, compileOptionRight, compileOptionWrong, configPath)
    batch_generate_logs(bugid, revision, configPath, compileOptionRight, compileOptionWrong)
    generate_abstract_summary(bugid,configPath)
    fileRank(bugid, configPath)
    function_FL(bugid, configPath, revision, compileOptionRight, compileOptionWrong)

    return bugid


with ProcessPoolExecutor(max_workers=4) as executor:
    # 提交所有任务
    future_to_bugid = {
        executor.submit(process_bugid_wrapper, (
            i, bugIds, revisions, compileOptionRights, compileOptionWrongs, checks, configPath, passBasePath
        )): bugIds[i]
        for i in range(len(bugIds))
    }

    # 收集结果
    completed_count = 0
    total_count = len(bugIds)

    for future in as_completed(future_to_bugid):
        bugid = future_to_bugid[future]
        try:
            result = future.result()
            completed_count += 1
            print(f"进度: {completed_count}/{total_count} - BugID {bugid} 处理完成")
        except Exception as e:
            completed_count += 1
            print(f"进度: {completed_count}/{total_count} - BugID {bugid} 处理失败: {e}")
