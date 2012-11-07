#!/usr/bin/env python
 
import os
import sys
import subprocess

import configure

# Thanks to http://patorjk.com/software/taag/  
# and http://www.chris.com/ascii/index.php?art=creatures/dragons
# for ASCII art inspiriation

pyne_logo = """\

                                  /   \       
 _                        )      ((   ))     (                          
(@)                      /|\      ))_((     /|\                          
|-|                     / | \    (/\|/\)   / | \                      (@) 
| | -------------------/--|-voV---\`|'/--Vov-|--\---------------------|-|
|-|                         '^`   (o o)  '^`                          | |
| |                               `\Y/'                               |-|
|-|                                                                   | |
| |        /\             ___           __  __             /\         |-|
|-|       /^~\           / _ \_   _  /\ \ \/__\           /^~\        | |  
| |       /^~\          / /_)/ | | |/  \/ /_\             /^~\        |-|
|-|       /^~\         / ___/| |_| / /\  //__             /^~\        | | 
| |       ^||`         \/     \__, \_\ \/\__/             ^||`        |-|  
|-|        ||                |____/                        ||         | | 
| |       ====                                            ====        |-|
|-|                                                                   | |
| |                                                                   |-|
|-|___________________________________________________________________| |
(@)              l   /\ /         ( (       \ /\   l                `\|-|
                 l /   V           \ \       V   \ l                  (@)
                 l/                _) )_          \I                   
                                   `\ /'
                                     `  
"""

def main_body():
    if not os.path.exists('build'):
        os.mkdir('build')
    hdf5opt = [o.split('=')[1] for o in sys.argv if o.startswith('--hdf5=')]
    if 0 < len(hdf5opt):
        os.environ['HDF5_ROOT'] = hdf5opt[0]  # Expose to CMake
        sys.argv = [o for o in sys.argv if not o.startswith('--hdf5=')]
    makefile = os.path.join('build', 'Makefile')
    if not os.path.exists(makefile):
        cmake_cmd = ['cmake', '..']
        if os.name == 'nt':
            files_on_path = set()
            for p in os.environ['PATH'].split(';')[::-1]:
                if os.path.exists(p):
                    files_on_path.update(os.listdir(p))
            if 'cl.exe' in files_on_path:
                pass
            elif 'sh.exe' in files_on_path:
                cmake_cmd += ['-G "MSYS Makefiles"']
            elif 'gcc.exe' in files_on_path:
                cmake_cmd += ['-G "MinGW Makefiles"']
            cmake_cmd = ' '.join(cmake_cmd)
        rtn = subprocess.check_call(cmake_cmd, cwd='build', shell=(os.name=='nt'))
    rtn = subprocess.check_call(['make'], cwd='build')
    cwd = os.getcwd()
    os.chdir('build')
    configure.setup()
    os.chdir(cwd)

def main():
    success = False
    try:
        main_body()
        success = True
    finally:
        configure.final_message(success)

if __name__ == "__main__":
    main()
