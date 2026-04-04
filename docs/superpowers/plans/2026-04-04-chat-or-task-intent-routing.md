# Chat-Or-Task Intent Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在单一 `POST /api/v1/messages` 下支持“普通聊天”与“任务执行”双模式，并保留现有审批+ReAct+Function Calling 执行链路。

**Architecture:** 在消息入口新增 `intent -> route -> response` 轻量编排层。`chat` 路径生成并落库 assistant 回复；`task` 路径复用现有 task 创建/审批/执行逻辑。高置信执行意图先走文本确认，再创建任务，避免误触发。

**Tech Stack:** FastAPI、SQLAlchemy、Pydantic v2、现有 `LLMClient`、pytest。

---

### Task 1: 扩展配置与消息响应契约

**Files:**
- Modify: `packages/domain/config.py`
- Modify: `packages/schemas/message.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: 为消息路由新增配置默认值（先写测试）**

```python
# tests/test_config.py

def test_intent_router_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIS_AGENT_INTENT_ROUTER_ENABLED", raising=False)
    monkeypatch.delenv("GIS_AGENT_INTENT_TASK_CONFIDENCE_THRESHOLD", raising=False)

    settings = Settings()

    assert settings.intent_router_enabled is True
    assert settings.intent_task_confidence_threshold == 0.75
```

- [ ] **Step 2: 运行单测，确认失败**

Run: `uv run pytest -q tests/test_config.py::test_intent_router_defaults`  
Expected: FAIL，提示 `Settings` 缺少新增字段。

- [ ] **Step 3: 在配置中补齐字段并设置安全默认值**

```python
# packages/domain/config.py
intent_router_enabled: bool = True
intent_task_confidence_threshold: float = 0.75
intent_history_limit: int = 8
intent_confirmation_keywords: str = "开始执行,按这个执行,确认执行,就按这个来"
```

- [ ] **Step 4: 扩展消息响应模型支持 chat/task 双模式**

```python
# packages/schemas/message.py
from typing import Literal

MessageMode = Literal["chat", "task"]
MessageIntent = Literal["chat", "task", "ambiguous"]

class MessageCreateResponse(BaseModel):
    message_id: str
    mode: MessageMode = "task"
    task_id: str | None = None
    task_status: str | None = None
    need_clarification: bool = False
    need_approval: bool = False
    missing_fields: list[str] = Field(default_factory=list)
    clarification_message: str | None = None
    assistant_message: str | None = None
    intent: MessageIntent | None = None
    intent_confidence: float | None = None
    awaiting_task_confirmation: bool = False
```

- [ ] **Step 5: 回归配置测试并提交**

Run: `uv run pytest -q tests/test_config.py`  
Expected: PASS。  
Commit:

```bash
git add packages/domain/config.py packages/schemas/message.py tests/test_config.py
git commit -m "feat: add intent-routing config and message response schema"
```

### Task 2: 新增意图识别与聊天回复服务

**Files:**
- Create: `packages/domain/services/intent.py`
- Create: `packages/domain/services/chat.py`
- Create: `packages/domain/prompts/intent.md`
- Create: `packages/domain/prompts/chat.md`
- Test: `tests/test_intent_service.py`

- [ ] **Step 1: 先写意图服务测试（规则降级 + 确认词检测）**

```python
# tests/test_intent_service.py

def test_detect_confirmation_keywords() -> None:
    assert is_task_confirmation_message("好的，开始执行") is True
    assert is_task_confirmation_message("我们再讨论一下") is False


def test_classify_intent_falls_back_to_ambiguous_on_llm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("packages.domain.services.intent.LLMClient.chat_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    result = classify_message_intent("帮我看看这个方案", history=[])
    assert result.intent == "ambiguous"
    assert result.confidence == 0.0
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `uv run pytest -q tests/test_intent_service.py`  
Expected: FAIL，`intent.py`/函数未定义。

- [ ] **Step 3: 实现意图识别服务**

```python
# packages/domain/services/intent.py
@dataclass(slots=True)
class IntentResult:
    intent: Literal["chat", "task", "ambiguous"]
    confidence: float
    reason: str


def is_task_confirmation_message(message: str) -> bool:
    keywords = _load_confirmation_keywords()
    normalized = message.strip().lower()
    return any(word in normalized for word in keywords)


def classify_message_intent(message: str, *, history: list[dict[str, str]], task_id: str | None = None, db_session: Session | None = None) -> IntentResult:
    normalized = message.strip()
    if is_task_confirmation_message(normalized):
        return IntentResult(intent="task", confidence=1.0, reason="confirmation_keyword")

    heuristic_score = _heuristic_task_score(normalized)
    if heuristic_score >= 0.90:
        return IntentResult(intent="task", confidence=heuristic_score, reason="heuristic_task_pattern")

    client = LLMClient(get_settings())
    try:
        response = client.chat_json(
            system_prompt=_load_intent_system_prompt(),
            user_prompt=_build_intent_user_prompt(message=normalized, history=history),
            phase="intent",
            task_id=task_id,
            db_session=db_session,
        )
        payload = LLMIntentResult.model_validate(response.content_json)
        return IntentResult(intent=payload.intent, confidence=float(payload.confidence), reason=payload.reason)
    except Exception:
        return IntentResult(intent="ambiguous", confidence=0.0, reason="intent_llm_failed")
```

- [ ] **Step 4: 实现聊天回复服务（LLM + 失败兜底）**

```python
# packages/domain/services/chat.py

def generate_chat_reply(*, user_message: str, history: list[dict[str, str]], task_id: str | None = None, db_session: Session | None = None) -> str:
    client = LLMClient(get_settings())
    system_prompt = _load_chat_system_prompt()
    user_prompt = _build_chat_user_prompt(user_message=user_message, history=history)
    try:
        response = client.chat_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            phase="chat",
            task_id=task_id,
            db_session=db_session,
        )
        return str(response.content_json.get("reply") or "")
    except Exception:
        return "我理解你的意思了。你可以继续补充目标、数据范围，或直接说“开始执行”。"
```

- [ ] **Step 5: 跑服务层测试并提交**

Run: `uv run pytest -q tests/test_intent_service.py`  
Expected: PASS。  
Commit:

```bash
git add packages/domain/services/intent.py packages/domain/services/chat.py packages/domain/prompts/intent.md packages/domain/prompts/chat.md tests/test_intent_service.py
git commit -m "feat: add intent classification and chat reply services"
```

### Task 3: 在 orchestrator 增加消息路由编排

**Files:**
- Modify: `packages/domain/services/orchestrator.py`
- Modify: `apps/api/routers/messages.py`
- Test: `tests/test_plan_approval_flow.py`
- Test: `tests/test_message_intent_routing.py` (new)

- [ ] **Step 1: 新建路由级集成测试（先失败）**

```python
# tests/test_message_intent_routing.py

def test_chat_message_returns_mode_chat_and_persists_assistant_message(client: TestClient):
    session_id = _create_session()
    payload = client.post(
        "/api/v1/messages",
        json={"session_id": session_id, "content": "先帮我比较一下 Sentinel-2 和 Landsat 的区别", "file_ids": []},
    ).json()
    assert payload["mode"] == "chat"
    assert payload["task_id"] is None
    assert payload["assistant_message"]


def test_task_intent_without_confirmation_returns_confirmation_prompt(client: TestClient):
    session_id = _create_session()
    payload = client.post(
        "/api/v1/messages",
        json={"session_id": session_id, "content": "用 Sentinel-2 做北京西山 2024 年 6 月 NDVI", "file_ids": []},
    ).json()
    assert payload["mode"] == "chat"
    assert payload["awaiting_task_confirmation"] is True


def test_confirmation_message_creates_task_and_enters_awaiting_approval(client: TestClient):
    session_id = _create_session()
    client.post(
        "/api/v1/messages",
        json={"session_id": session_id, "content": "用 Sentinel-2 做北京西山 2024 年 6 月 NDVI", "file_ids": []},
    )
    payload = client.post(
        "/api/v1/messages",
        json={"session_id": session_id, "content": "开始执行", "file_ids": []},
    ).json()
    assert payload["mode"] == "task"
    assert payload["task_status"] == "awaiting_approval"
```

- [ ] **Step 2: 运行新增测试，确认失败**

Run: `uv run pytest -q tests/test_message_intent_routing.py`  
Expected: FAIL，当前 `POST /messages` 仅返回 task 流程。

- [ ] **Step 3: 在 orchestrator 引入统一入口路由**

```python
# packages/domain/services/orchestrator.py

def create_message(db: Session, payload: MessageCreateRequest) -> MessageCreateResponse:
    user_message = _create_user_message(db, session_id=payload.session_id, content=payload.content)
    history = _list_recent_session_messages(db, session_id=payload.session_id, limit=get_settings().intent_history_limit)
    intent_result = classify_message_intent(payload.content, history=history, task_id=None, db_session=db)
    confirmed = is_task_confirmation_message(payload.content)

    if intent_result.intent == "task" and intent_result.confidence >= get_settings().intent_task_confidence_threshold and confirmed:
        return _create_task_from_message_record(db=db, payload=payload, user_message=user_message)

    if intent_result.intent == "task" and intent_result.confidence >= get_settings().intent_task_confidence_threshold:
        assistant_text = "我可以按这个方案为你创建任务。请回复“开始执行”确认。"
        _create_assistant_message(db, session_id=payload.session_id, content=assistant_text)
        db.commit()
        return MessageCreateResponse(
            message_id=user_message.id,
            mode="chat",
            assistant_message=assistant_text,
            intent="task",
            intent_confidence=float(intent_result.confidence),
            awaiting_task_confirmation=True,
        )

    assistant_text = generate_chat_reply(user_message=payload.content, history=history, task_id=None, db_session=db)
    _create_assistant_message(db, session_id=payload.session_id, content=assistant_text)
    db.commit()
    return MessageCreateResponse(
        message_id=user_message.id,
        mode="chat",
        assistant_message=assistant_text,
        intent=intent_result.intent,
        intent_confidence=float(intent_result.confidence),
        awaiting_task_confirmation=False,
    )
```

实现细节：
- 保留现有 `create_message_and_task` 作为任务创建内部函数，避免影响既有回归。
- 新增 `_create_assistant_message`、`_list_recent_session_messages` 工具函数。
- 对“确认词但缺少上下文”的情况返回 `mode=chat` + 指引文案，不抛错。

- [ ] **Step 4: 将消息路由切换到新入口**

```python
# apps/api/routers/messages.py
from packages.domain.services.orchestrator import create_message

@router.post("/messages", response_model=MessageCreateResponse, responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}})
def create_message_endpoint(payload: MessageCreateRequest, db: Session = Depends(get_db)) -> MessageCreateResponse:
    return create_message(db=db, payload=payload)
```

- [ ] **Step 5: 跑消息与审批回归并提交**

Run:
- `uv run pytest -q tests/test_message_intent_routing.py`
- `uv run pytest -q tests/test_plan_approval_flow.py`

Expected: PASS。  
Commit:

```bash
git add packages/domain/services/orchestrator.py apps/api/routers/messages.py tests/test_message_intent_routing.py tests/test_plan_approval_flow.py
git commit -m "feat: route /messages between chat and task flows"
```

### Task 4: 补齐 assistant 消息落库与会话回放断言

**Files:**
- Modify: `packages/domain/services/orchestrator.py`
- Test: `tests/test_message_intent_routing.py`

- [ ] **Step 1: 写失败测试，断言 assistant 消息入库**

```python
# tests/test_message_intent_routing.py
with SessionLocal() as db:
    rows = db.query(MessageRecord).filter(MessageRecord.session_id == session_id).order_by(MessageRecord.created_at.asc()).all()
assert [row.role for row in rows][-1] == "assistant"
```

- [ ] **Step 2: 运行该用例确认失败**

Run: `uv run pytest -q tests/test_message_intent_routing.py::test_chat_message_returns_mode_chat_and_persists_assistant_message`  
Expected: FAIL，尚未落库 assistant。

- [ ] **Step 3: 实现 assistant 落库与可回放历史读取**

```python
# packages/domain/services/orchestrator.py

def _create_assistant_message(db: Session, *, session_id: str, content: str, linked_task_id: str | None = None) -> MessageRecord:
    record = MessageRecord(id=make_id("msg"), session_id=session_id, role="assistant", content=content, linked_task_id=linked_task_id)
    db.add(record)
    db.flush()
    return record
```

- [ ] **Step 4: 回归消息路由测试**

Run: `uv run pytest -q tests/test_message_intent_routing.py`  
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add packages/domain/services/orchestrator.py tests/test_message_intent_routing.py
git commit -m "feat: persist assistant replies for chat-mode messages"
```

### Task 5: 全量回归与任务清单同步

**Files:**
- Modify: `开发任务清单.md`

- [ ] **Step 1: 运行后端全量检查**

Run:
- `uv run python -m compileall apps packages tests`
- `uv run ruff check`
- `uv run pytest -q`

Expected: 全通过。

- [ ] **Step 2: 补三轮稳定性抽样（意图路由相关）**

Run:

```bash
for i in 1 2 3; do
  uv run pytest -q tests/test_message_intent_routing.py tests/test_plan_approval_flow.py
done
```

Expected: 三轮稳定 PASS。

- [ ] **Step 3: 更新清单状态与完成说明**

```markdown
- [x] BE-50 聊天/执行意图路由
- [x] BE-51 执行确认门槛
- [x] BE-52 assistant 消息落库与回放
```

- [ ] **Step 4: 提交文档变更**

```bash
git add "开发任务清单.md"
git commit -m "docs: record intent-routing backend tasks completion"
```

- [ ] **Step 5: 记录最终验收命令结果到提交说明**

在最终汇报中包含：
- 全量 pytest 通过数
- 关键路由测试用例名
- 与现有审批/ReAct 回归无冲突结论

## Plan Self-Review

1. **Spec 覆盖检查**：已覆盖单路由自动分流、不确定走 chat、chat 走 LLM、高置信先确认、文本确认触发执行、assistant 入库回放。  
2. **占位符检查**：无 `TODO/TBD` 与“后续补充”式步骤。  
3. **一致性检查**：响应字段、服务命名、测试文件命名与任务描述一致。
