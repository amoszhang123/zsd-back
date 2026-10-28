import base64
import io

import qrcode


def render_qr_data_uri(text: str) -> str:
    """把文本渲染成 PNG 二维码的 data URI，前端直接当 img src 用。

    单独抽一个模块是因为两处要用：小程序的员工身份码（routers/mp.py）和
    web 扫码登录码（routers/auth.py），而 mp.py 已经 import 了 auth.py，
    放在任何一方都会成环。
    """
    img = qrcode.make(
        text,
        error_correction=qrcode.constants.ERROR_CORRECT_Q,
        box_size=8,
        border=2,
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
