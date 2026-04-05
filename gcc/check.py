import os
import re
import subprocess
import time
from configparser import ConfigParser


def checkIsPass_wrongcodeOneline(configFile, revisionNumber,compilationOptionsRight,compilationOptionsWrong): # change per bug

    cfg = ConfigParser()
    cfg.read(configFile)
    compilersdir = cfg.get('gcc-locations', 'compilersdir')
    prefixpath = compilersdir + revisionNumber + '/' + revisionNumber

    gccpath=prefixpath+'-build/bin/gcc'
    covdir=prefixpath+'-build/gcc'

    if os.path.exists('a.out'):
        os.system('rm a.out')
    os.system('find '+covdir+' -name \"*.gcda\" | xargs rm -f')
    # exccmd(gccpath+' '+compilationOptionsRight+' mainvar.c')
    err_set = set()
    os.system('{ ' + gccpath + ' ' + compilationOptionsRight + ' mainvar.c' + ' ;}' + ' >comp_output.txt 2>&1')
    print('\033[1;35m --------------------compiler log begin' '\033[0m')
    os.system("cat comp_output.txt")
    print('\033[1;35m --------------------compiler log end' '\033[0m')
    lines = ''
    with open('comp_output.txt', 'r') as file:
        for line in file:
            lines += line
    regex = re.compile(r'^.*\berror\w*\b.*$', re.MULTILINE)
    # Find all the matches in the string and add them to a set
    matches = {match.strip() for match in regex.findall(lines)}
    err_set.update(matches)
    regex = re.compile(r'^.*\bundefined\w*\b.*$', re.MULTILINE)
    # Find all the matches in the string and add them to a set
    matches = {match.strip() for match in regex.findall(lines)}
    err_set.update(matches)
    if not os.path.exists('a.out'):
        return 0, err_set # compilation error

    if os.path.exists('rightfile'):
        os.system('rm rightfile')

    start=time.time()
    # os.system('timeout 10 ./a.out 2>&1 | tee rightfile')
    os.system('{ timeout 10 ./a.out ; } >rightfile 2>&1')
    end=time.time()
    if (end-start)>=10:
        return 0, err_set

    f=open('rightfile')
    lines=f.readlines()
    f.close()
    if len(lines)!=1:
        return 0,err_set
    else:
        if 'core dumped' in lines[0] or 'dumped core' in lines[0]:
            return 0,err_set

    if os.path.exists('a.out'):
        os.system('rm a.out')
    os.system('find '+covdir+' -name \"*.gcda\" | xargs rm -f')
    os.system(gccpath+' '+compilationOptionsWrong+' mainvar.c')
    if not os.path.exists('a.out'):
        return 0, err_set

    if os.path.exists('wrongfile'):
        os.system('rm wrongfile')
    start=time.time()
    # os.system('timeout 10 ./a.out 2>&1 | tee wrongfile')
    os.system('{ timeout 10 ./a.out ; } >wrongfile 2>&1')
    end=time.time()
    if (end-start)>=10:
        return 0, err_set

    f=open('wrongfile')
    lines=f.readlines()
    f.close()
    if len(lines)!=1:
        return 0, err_set
    else:
        if 'core dumped' in lines[0] or 'dumped core' in lines[0]:
            return 0, err_set

    if os.path.exists('diffwr'):
        os.system('rm diffwr')
    os.system('diff wrongfile rightfile > diffwr')
    f=open('diffwr')
    lines=f.readlines()
    f.close()
    if len(lines)==0:
        return 1, err_set # pass
    else:
        return 2, err_set # still fail

def checkIsPass_onenumberandzero(configFile, revisionNumber,compilationOptionsRight,compilationOptionsWrong): # change per bug

    cfg = ConfigParser()
    cfg.read(configFile)
    compilersdir = cfg.get('gcc-locations', 'compilersdir')
    prefixpath = compilersdir + revisionNumber + '/' + revisionNumber

    gccpath=prefixpath+'-build/bin/gcc'
    covdir=prefixpath+'-build/gcc'

    if os.path.exists('a.out'):
        os.system('rm a.out')
    os.system('find '+covdir+' -name \"*.gcda\" | xargs rm -f')
    # os.system(gccpath+' '+compilationOptionsRight+' mainvar.c')
    err_set = set()
    os.system('{ ' + gccpath + ' ' + compilationOptionsRight + ' mainvar.c' + ' ;}' + ' >comp_output.txt 2>&1')
    print('\033[1;35m --------------------compiler log begin' '\033[0m')
    os.system("cat comp_output.txt")
    print('\033[1;35m --------------------compiler log end' '\033[0m')
    lines = ''
    with open('comp_output.txt', 'r') as file:
        for line in file:
            lines += line
    regex = re.compile(r'^.*\berror\w*\b.*$', re.MULTILINE)
    # Find all the matches in the string and add them to a set
    matches = {match.strip() for match in regex.findall(lines)}
    err_set.update(matches)
    regex = re.compile(r'^.*\bundefined\w*\b.*$', re.MULTILINE)
    # Find all the matches in the string and add them to a set
    matches = {match.strip() for match in regex.findall(lines)}
    err_set.update(matches)
    if not os.path.exists('a.out'):
        return 0, err_set

    if os.path.exists('rightfile'):
        os.system('rm rightfile')

    start=time.time()
    # os.system('timeout 10 ./a.out 2>&1 | tee rightfile')
    os.system('{ timeout 10 ./a.out ; } >rightfile 2>&1')
    end=time.time()
    if (end-start)>=10:
        return 0, err_set

    f=open('rightfile')
    lines=f.readlines()
    f.close()
    # if len(lines)!=1: original if condition
    if len(lines) < 1:
        return 0, err_set
    if 'core dumped' in lines[0] or 'dumped core' in lines[0] or 'exception' in lines[0] or 'Abort' in lines[0] or 'Segmentation' in lines[0]:
        return 0, err_set

    if os.path.exists('a.out'):
        os.system('rm a.out')
    os.system('find ' + covdir + ' -name \"*.gcda\" | xargs rm -f')
    os.system(gccpath + ' ' + compilationOptionsWrong + ' mainvar.c')
    if not os.path.exists('a.out'):
        return 0, err_set

    if os.path.exists('wrongfile'):
        os.system('rm wrongfile')
    start = time.time()
    # os.system('timeout 10 ./a.out 2>&1 | tee wrongfile')
    os.system('{ timeout 10 ./a.out ; } >wrongfile 2>&1')
    end = time.time()
    if (end - start) >= 10:
        return 0, err_set

    f = open('wrongfile')
    lines = f.readlines()
    f.close()
    # if len(lines)!=1:
    #     return 0

    if os.path.exists('diffwr'):
        os.system('rm diffwr')
    os.system('diff wrongfile rightfile > diffwr')
    f = open('diffwr')
    diffmesslines = f.readlines()
    f.close()
    if len(diffmesslines) == 0:
        return 1, err_set  # pass
    else:
        if len(lines) == 0:
            return 2, err_set  # still fail
        else:
            return 0, err_set


def checkIsPass_zeroandsegmentoneline(configFile, revisionNumber,compilationOptionsRight,compilationOptionsWrong): # change per bug

    cfg = ConfigParser()
    cfg.read(configFile)
    compilersdir = cfg.get('gcc-locations', 'compilersdir')
    prefixpath = compilersdir + revisionNumber + '/' + revisionNumber

    gccpath=prefixpath+'-build/bin/gcc'
    covdir=prefixpath+'-build/gcc'

    if os.path.exists('a.out'):
        os.system('rm a.out')
    os.system('find '+covdir+' -name \"*.gcda\" | xargs rm -f')
    # os.system(gccpath+' '+compilationOptionsRight+' mainvar.c')
    err_set = set()
    print('{ ' + gccpath + ' ' + compilationOptionsRight + ' mainvar.c' + ' ;}' + ' >comp_output.txt 2>&1')
    os.system('{ ' + gccpath + ' ' + compilationOptionsRight + ' mainvar.c' + ' ;}' + ' >comp_output.txt 2>&1')
    print('\033[1;35m --------------------compiler log begin' '\033[0m')
    os.system("cat comp_output.txt")
    print('\033[1;35m --------------------compiler log end' '\033[0m')
    lines = ''
    with open('comp_output.txt', 'r') as file:
        for line in file:
            lines += line
    regex = re.compile(r'^.*\berror\w*\b.*$', re.MULTILINE)
    # Find all the matches in the string and add them to a set
    matches = {match.strip() for match in regex.findall(lines)}
    err_set.update(matches)
    regex = re.compile(r'^.*\bundefined\w*\b.*$', re.MULTILINE)
    # Find all the matches in the string and add them to a set
    matches = {match.strip() for match in regex.findall(lines)}
    err_set.update(matches)
    if not os.path.exists('a.out'):
        return 0, err_set

    if os.path.exists('rightfile'):
        os.system('rm rightfile')

    start=time.time()
    # os.system('timeout 10 ./a.out 2>&1 | tee rightfile')
    os.system('{ timeout 10 ./a.out ; } >rightfile 2>&1')
    end=time.time()
    if (end-start)>=10:
        return 0, err_set

    f=open('rightfile')
    lines=f.readlines()
    f.close()
    if len(lines)!=0:
        print("rightfile != 0?")
        print("rightfile : ", lines)
        return 0, err_set

    if os.path.exists('a.out'):
        os.system('rm a.out')
    os.system('find '+covdir+' -name \"*.gcda\" | xargs rm -f')
    os.system(gccpath+' '+compilationOptionsWrong+' mainvar.c')
    if not os.path.exists('a.out'):
        return 0, err_set

    if os.path.exists('wrongfile'):
        os.system('rm wrongfile')
    start=time.time()
    # os.system('timeout 10 ./a.out 2>&1 | tee wrongfile')
    os.system('{ timeout 10 ./a.out ; } >wrongfile 2>&1')
    end=time.time()
    if (end-start)>=10:
        return 0, err_set

    f=open('wrongfile')
    lines=f.readlines()
    f.close()
    # if len(lines)!=1:
    #     return 0

    if os.path.exists('diffwr'):
        os.system('rm diffwr')
    os.system('diff wrongfile rightfile > diffwr')
    f=open('diffwr')
    diffmesslines=f.readlines()
    f.close()

    if os.path.exists('diffow'):
        os.system('rm diffow')
    os.system('diff wrongfile oriwrongfile > diffow')
    f=open('diffow')
    diffowlines=f.readlines()
    f.close()
    print("diffowlines : ", diffowlines)
    print("diffmesslines : ", diffmesslines)
    if len(diffmesslines)==0:
        print("diffmesslines != 0?")
        return 1, err_set # pass
    else:
        if len(diffowlines)==0: # 'core dumped' in lines[0]:
            return 2, err_set # still fail
        else:
            return 0, err_set


def file_contains_ice_error(filename):
    with open(filename, 'r') as file:
        contents = file.read()
        if contents.find('internal compiler error') != -1:
            return True
        else:
            return False


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


def checkIsPass_crash(configFile, revisionNumber, compilationOptionsWrong):
    cfg = ConfigParser()
    cfg.read(configFile)
    compilersdir = cfg.get('gcc-locations', 'compilersdir')
    prefixpath = compilersdir + revisionNumber + '/' + revisionNumber

    gccpath = prefixpath + '-build/bin/gcc'
    covdir = prefixpath + '-build/gcc'
    # print("cmd for crash : ", gccpath+' '+compilationOptionsWrong+' mainvar.c')
    os.system('find ' + covdir + ' -name \"*.gcda\" | xargs rm -f')

    err_set = set()
    os.system('{ ' + gccpath + ' ' + compilationOptionsWrong + ' mainvar.c' + ' ;}' + ' >comp_output.txt 2>&1')
    print('\033[1;35m --------------------compiler log begin' '\033[0m')
    os.system("cat comp_output.txt")
    print('\033[1;35m --------------------compiler log end' '\033[0m')
    lines = ''
    with open('comp_output.txt', 'r') as file:
        for line in file:
            lines += line
    regex = re.compile(r'^.*\berror\w*\b.*$', re.MULTILINE)
    # Find all the matches in the string and add them to a set
    matches = {match.strip() for match in regex.findall(lines)}
    err_set.update(matches)
    regex = re.compile(r'^.*\bundefined\w*\b.*$', re.MULTILINE)
    # Find all the matches in the string and add them to a set
    matches = {match.strip() for match in regex.findall(lines)}
    err_set.update(matches)

    result = subprocess.getstatusoutput(gccpath+' '+compilationOptionsWrong+' mainvar.c')

    # exccmd('find ' + srcdir + ' -name \"*.c\" > gcdalist')
    # exccmd('find ' + srcdir + ' -name \"*.h\" >> gcdalist')
    exccmd('find ' + covdir + ' -name \"*.gcda\" > gcdalist')
    # os.system("cat crash_output.txt")
    # result = subprocess.getstatusoutput("grep error crash_output.txt; echo $?")
    print("return from checkIsPass_crash : ", result[0])
    if result[0] == 0 and file_contains_ice_error("./comp_output.txt") == False:
        return 1, err_set  # pass
    else:
        return 2, err_set  # fail