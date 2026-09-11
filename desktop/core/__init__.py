"""桌面端的非界面层：路径、后端宿主、HTTP 客户端、偏好、音效。

这里**不允许 import 任何 QtWidgets**（`settings.py`/`api.py` 只用 QtCore/QtNetwork），
这样这一层可以被 pytest 直接测，不需要先把窗口开起来。
"""
