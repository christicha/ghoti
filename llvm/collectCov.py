import os.path
from configparser import ConfigParser


def collectcov(bugid, revision, resultPath, configPath):
    config = ConfigParser()
    config.read(configPath)
    compilerBasePath = config.get('llvm-locations', 'compilersdir')
    passdir = config.get('llvm-locations', 'passdir')
    buglist = config.get('llvm-locations', 'bugList')
    workpath = passdir + '/' + bugid
    os.chdir(workpath)
    covdir = compilerBasePath + revision + '/' + revision + '-build'
    if os.path.exists('gcdalist'):
        os.system('rm gcdalist')
    os.system('find ' + covdir + ' -name \"*.gcda\" > gcdalist')
    f = open('gcdalist')
    lines = f.readlines()
    f.close()
    if not os.path.exists(resultPath):
        os.system('mkdir -p ' + resultPath)
    methodfile = open(resultPath + '/method_info.txt', 'w')
    stmtfile = open(resultPath + '/stmt_info.txt', 'w')

    for i in range(len(lines)):
        gcdafile = lines[i].strip()
        if '/clang/test/' in gcdafile:
            continue
        os.system('rm *.gcov')
        if os.path.exists('gcovfile'):
            os.system('rm gcovfile')
        os.system('gcov -f ' + gcdafile + ' > gcovfile')
        if not os.path.exists('./' + gcdafile.strip().split('/')[-1].split('.gcda')[0] + '.gcov'):
            continue

        f = open('gcovfile')
        gcovlines = f.readlines()
        f.close()

        for j in range(len(gcovlines)):
            if 'Function \'' in gcovlines[j].strip():
                if 'Lines executed:' in gcovlines[j + 1].strip() and float(
                        gcovlines[j + 1].strip().split('Lines executed:')[1].split('%')[0].strip()) != 0.0:
                    methodfile.write(
                        gcdafile.split(covdir + '/')[-1] + ',' + gcovlines[j].strip().split('\'')[1] + ',' +
                        gcovlines[j + 1].strip().split('Lines executed:')[1].split('%')[0].strip() + ',' +
                        gcovlines[j + 1].strip().split('of')[-1].strip() + '\n')
        f = open(gcdafile.strip().split('/')[-1].split('.gcda')[0] + '.gcov')
        stmtlines = f.readlines()
        f.close()
        tmp = []
        for j in range(len(stmtlines)):
            if stmtlines[j] == '------------------\n':
                continue
            covcnt = stmtlines[j].strip().split(':')[0].strip()
            linenum = stmtlines[j].strip().split(':')[1].strip()
            if covcnt != '-' and covcnt != '#####':
                tmp.append(linenum)
        if len(tmp) == 0:
            continue
        stmtfile.write(gcdafile.split(covdir + '/')[-1] + ':' + ','.join(tmp) + '\n')
