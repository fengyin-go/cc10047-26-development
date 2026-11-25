"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import chains, quality_gate, resource

# 质量门禁路由带固定前缀，需先于通用 /api/{module} 资源路由注册。
ROUTERS = [quality_gate, resource, chains]
