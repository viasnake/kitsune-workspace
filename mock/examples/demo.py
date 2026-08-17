from __future__ import annotations

import asyncio
import json

from kitsune_mock import Application, MockWorkspace, UsagePlugin


async def main() -> None:
    workspace = MockWorkspace()

    sre = Application("sre-agent")
    sre_usage = UsagePlugin()
    sre.use(sre_usage)

    @sre.handler()
    async def handle_sre(ctx, request):
        # 実際には Pydantic AI 等を呼ぶ場所。
        await sre.emit(
            "model.usage",
            run_id=ctx.run_id,
            correlation_id=ctx.correlation_id,
            payload={"units": 120},
        )
        return {"answer": f"調査要求を受け付けました: {request['message']}"}

    knowledge = Application("knowledge-agent")

    @knowledge.handler()
    async def handle_knowledge(ctx, request):
        return {"updated": True, "target": request.get("target", "default")}

    workspace.register(sre)
    workspace.register(knowledge)

    await workspace.start_all()

    sre_result = await workspace.invoke(
        "sre-agent",
        {"message": "API の応答時間上昇を調査して"},
    )
    knowledge_result = await knowledge.invoke(
        {"target": "operations-knowledge"},
        source="schedule",
    )

    print("SRE result:")
    print(json.dumps(sre_result, ensure_ascii=False, indent=2))
    print("Knowledge result:")
    print(json.dumps(knowledge_result, ensure_ascii=False, indent=2))
    print("Workspace status:")
    print(json.dumps(workspace.status(), ensure_ascii=False, indent=2))
    print("SRE usage units:", sre_usage.total_units)

    await workspace.stop_all()


if __name__ == "__main__":
    asyncio.run(main())
