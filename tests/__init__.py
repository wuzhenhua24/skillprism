"""让 ``tests`` 成为一个包——``from tests.conftest import ...`` 靠它才不挑调用方式。

没有这个文件时，pytest 只把 ``tests/`` 放上 ``sys.path``、不放仓库根，
``import tests`` 成不成全看有没有别人把仓库根带上：``python -m pytest`` 恰好带了
当前目录，裸 ``pytest`` 没有，收集期直接中断、一个用例都不跑。而且即使能跑，
conftest 也被加载两份（pytest 按 ``conftest``，测试模块按 ``tests.conftest``）。
有了它，pytest 自己把仓库根放上去，conftest 只以 ``tests.conftest`` 加载一次。

仓库根上 ``sys.path`` 在 src 布局下是安全的：``skillprism`` 在 ``src/`` 里，
根目录没有能遮住已安装包的东西。
"""
