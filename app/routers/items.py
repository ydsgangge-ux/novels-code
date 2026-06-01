from typing import Optional
from fastapi import APIRouter, HTTPException, Query

router = APIRouter(tags=["items"])

# 内存数据存储
items_db: list[dict] = []
next_id: int = 1


@router.get("/items")
async def list_items(
    skip: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
):
    """获取物品列表，支持分页"""
    return {"items": items_db[skip : skip + limit], "total": len(items_db)}


@router.post("/items", status_code=201)
async def create_item(name: str, description: Optional[str] = None):
    """创建新物品"""
    global next_id
    item = {"id": next_id, "name": name, "description": description}
    items_db.append(item)
    next_id += 1
    return item


@router.get("/items/{item_id}")
async def get_item(item_id: int):
    """根据 ID 获取单个物品"""
    for item in items_db:
        if item["id"] == item_id:
            return item
    raise HTTPException(status_code=404, detail="Item not found")


@router.put("/items/{item_id}")
async def update_item(item_id: int, name: Optional[str] = None, description: Optional[str] = None):
    """更新物品信息"""
    for item in items_db:
        if item["id"] == item_id:
            if name is not None:
                item["name"] = name
            if description is not None:
                item["description"] = description
            return item
    raise HTTPException(status_code=404, detail="Item not found")


@router.delete("/items/{item_id}", status_code=204)
async def delete_item(item_id: int):
    """删除物品"""
    for i, item in enumerate(items_db):
        if item["id"] == item_id:
            items_db.pop(i)
            return
    raise HTTPException(status_code=404, detail="Item not found")
