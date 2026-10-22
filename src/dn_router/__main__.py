# 支持 python -m dn_router，与命令行脚本共用入口及退出码。
from .cli import main
raise SystemExit(main())
