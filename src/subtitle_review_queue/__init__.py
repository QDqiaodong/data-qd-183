"""字幕复核工单账本（subtitle-review-queue）。

离线命令 ``subtitle-review-queue --state STATE --source INPUT.json
--output REPORT.json`` 从一个已锁定节目版的普通对版输入登记待复核组，并在
独立账本里记录领取、交回、完成和原样重取。一个账本对应一个节目版；重新
剪辑须使用另一个账本，不迁移旧决定。仅依赖标准库，不联网、不读取媒体。
"""
