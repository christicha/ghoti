import os.path
import shutil
from configparser import ConfigParser

config = ConfigParser()
config.read('./config/config.ini', encoding='utf-8')
compilerBasePath = config.get('llvm-locations', 'compilersdir')
buglist = config.get('llvm-locations', 'bugList')
infoBasePath = config.get('llvm-locations', 'infodir')
resultBasePath =config.get('llvm-locations', 'resultDir')
fileListBasePath = config.get('llvm-locations', 'fileListDir')
benchmarkPath = config.get('llvm-locations', 'benchmark')

if not os.path.exists(infoBasePath):
    os.mkdir(infoBasePath)
if not os.path.exists(resultBasePath):
    os.mkdir(resultBasePath)
if not os.path.exists(fileListBasePath):
    os.mkdir(fileListBasePath)

bugIds = []
revisions = []
compileOptionRights = []
compileOptionWrongs = []



with open(buglist, 'r') as f:
    for line in f:
        line = line.strip()
        items = line.split(',')
        bugIds.append(items[0])
        revisions.append(items[1])
        compileOptionRights.append(items[2])
        compileOptionWrongs.append(items[3])


def exccmd(cmd):
    p = os.popen(cmd, "r")
    rs = []
    line = ""
    while True:
        line = p.readline()

        if not line:
            break
        # print line
        # rs.append(line.strip())
    p.close()
    return rs


def collect(compilersdir, infodir, revision, wrongoption):

    testname = 'fail'

    covdir = compilersdir
    gccdir = covdir + '/bin'
    resdir = infodir

    os.chdir(resdir)

    if os.path.exists(resdir + 'failcov' + '/method_info.txt') \
            and os.path.exists(resdir + '/' + 'failcov' + '/stmt_info.txt'):
        methodfile = open(resdir + '/' + 'failcov' + '/method_info.txt', 'r')
        methodlines = methodfile.readlines()
        methodfile.close()
        stmtfile = open(resdir + '/' + 'failcov' + '/stmt_info.txt', 'r')
        stmtlines = stmtfile.readlines()
        stmtfile.close()
        if len(stmtlines) > 0 and len(methodlines) > 0:
            return

    methodfile = open(resdir + 'failcov' + '/method_info.txt', 'w')
    stmtfile = open(resdir + 'failcov' + '/stmt_info.txt', 'w')
    # delete all .gcda files
    exccmd('find ' + covdir + ' -name \"*.gcda\" | xargs rm -f')
    # compile test program
    exccmd(gccdir + '/clang ' + wrongoption + ' ' + testname + '.c')  # change per bug
    if os.path.exists('oriwrongfile'):
        os.system('rm oriwrongfile')
    os.system('{ timeout 10 ./a.out; } >oriwrongfile 2>&1')

    if os.path.exists('gcdalist'):
        exccmd('rm gcdalist')
    exccmd('find ' + covdir + ' -name \"*.gcda\" > gcdalist')

    f = open('gcdalist')
    lines = f.readlines()
    f.close()

    for i in range(len(lines)):
        gcdafile = lines[i].strip()
        if '/clang/test/' in gcdafile:  # ?
            continue
        exccmd('rm *.gcov')
        if os.path.exists('gcovfile'):
            exccmd('rm gcovfile')
        exccmd('gcov -f ' + gcdafile + ' > gcovfile')
        if not os.path.exists('./' + gcdafile.strip().split('/')[-1].split('.gcda')[0] + '.gcov'):
            continue
        f = open('gcovfile')
        gcovlines = f.readlines()
        f.close()
        for j in range(len(gcovlines)):
            if 'Function \'' in gcovlines[j].strip():
                if 'Lines executed:' in gcovlines[j + 1].strip() and \
                        float(gcovlines[j + 1].strip().split('Lines executed:')[1].split('%')[0].strip()) != 0.0:
                    methodfile.write(gcdafile.split(covdir + '/')[-1] + ',' + gcovlines[j].strip().split('\'')[1]
                                     + ',' + gcovlines[j + 1].strip().split('Lines executed:')[1].split('%')[
                                         0].strip() +
                                     ',' + gcovlines[j + 1].strip().split('of')[-1].strip() + '\n')

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
    stmtfile.close()
    methodfile.close()


for i in range(len(bugIds)):
    bugid = bugIds[i]
    revision = revisions[i]
    compileOptionWrong = compileOptionWrongs[i]
    failcovPath = infoBasePath + bugid + '/'
    if not os.path.exists(failcovPath):
        os.makedirs(failcovPath)
        os.makedirs(failcovPath+'/failcov')
    sourcePath = benchmarkPath + bugid
    for item in os.listdir(sourcePath):
        filePath = os.path.join(sourcePath, item)
        if os.path.isfile(filePath):
            targetPath = os.path.join(failcovPath, item)
            shutil.copy2(filePath, targetPath)
    os.chdir(failcovPath)
    compilerPath = compilerBasePath + revision + '/' + revision + '-build'
    collect(compilerPath, failcovPath, revision, compileOptionWrong)
