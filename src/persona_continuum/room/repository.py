from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from persona_continuum.application._utils import dumps, loads, new_id
from persona_continuum.room.models import (
    ProtocolTask,
    RoomProtocolEvent,
    RoomProtocolState,
    RoomProtocolType,
    RoomRunStatus,
    RoomTemplate,
)
from persona_continuum.storage.database import Database

DIVINATION_TEMPLATE_ID = "room_template_divination_consultation"


class RoomProtocolRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def ensure_builtin_templates(self) -> None:
        # Built-in templates are refreshed on every startup: they are
        # immutable to users, and re-saving upgrades legacy rows in place
        # (role slots instead of hard-coded persona ids) without any
        # destructive migration.
        now = datetime.now(UTC)
        template = RoomTemplate.model_validate(
            {
                "id": DIVINATION_TEMPLATE_ID,
                "name": "术数综合会诊",
                "description": "通用专家会诊协议的示例模板；术数规则不属于 Room Engine。",
                "protocol": "expert_consultation",
                # Role slots: persona_id stays empty so the user maps each
                # slot (host / experts) to real personas at room creation.
                # No tool permissions anywhere: every expert owns its own
                # domain computation and interpretation skills.
                "participants": [
                    {
                        "participant_id": "slot_host",
                        "persona_id": "",
                        "display_name": "主持人",
                        "role": "host",
                        "authority": 100,
                    },
                    {
                        "participant_id": "slot_expert_bazi",
                        "persona_id": "",
                        "display_name": "八字专家",
                        "role": "expert",
                        "authority": 70,
                        "specialties": [
                            "四柱八字",
                            "子平命理",
                            "天干地支",
                            "十神",
                            "旺衰",
                            "格局",
                            "调候",
                            "大运",
                            "流年",
                            "事业",
                            "学业",
                            "财富",
                            "婚恋",
                            "长期人生结构",
                        ],
                    },
                    {
                        "participant_id": "slot_expert_ziwei",
                        "persona_id": "",
                        "display_name": "紫微专家",
                        "role": "expert",
                        "authority": 70,
                        "specialties": [
                            "紫微斗数",
                            "十四主星",
                            "四化",
                            "宫位",
                            "大限",
                            "流年",
                            "事业",
                            "财富",
                            "婚恋",
                            "中长期人生结构",
                        ],
                    },
                    {
                        "participant_id": "slot_expert_yigua",
                        "persona_id": "",
                        "display_name": "易卦专家",
                        "role": "expert",
                        "authority": 70,
                        "specialties": [
                            "六爻",
                            "梅花易数",
                            "小六壬",
                            "起卦",
                            "卦象",
                            "爻辞",
                            "用神",
                            "应期",
                            "择时",
                            "具体事件研判",
                        ],
                    },
                    {
                        "participant_id": "slot_expert_sanshi",
                        "persona_id": "",
                        "display_name": "三式专家",
                        "role": "expert",
                        "authority": 70,
                        "specialties": [
                            "奇门遁甲",
                            "大六壬",
                            "太乙神数",
                            "时空格局",
                            "谋事",
                            "决策时机",
                            "方位",
                            "竞争态势",
                        ],
                    },
                    {
                        "participant_id": "slot_expert_western",
                        "persona_id": "",
                        "display_name": "西学专家",
                        "role": "expert",
                        "authority": 70,
                        "specialties": [
                            "西方占星",
                            "塔罗",
                            "本命盘",
                            "行运",
                            "推运",
                            "心理原型",
                            "象征解读",
                        ],
                    },
                ],
                "topic": "综合命理会诊：人生关键决策的多视角研判",
                "shared_context": {
                    "background": (
                        "这是一个多术数体系联合会诊房间。主持人负责判断问题适合使用哪一种或"
                        "哪几种体系。专家只对自己的专业体系负责。不同体系先独立判断，再进行"
                        "交叉比较。不能为了制造一致性强行统一不同体系。每位专家本身掌握自己"
                        "的专业计算、推演与解释方法。最终由主持人解释共同点、冲突点、不确定性"
                        "与现实建议。"
                    )
                },
                "protocol_config": {
                    "routing_mode": "host_decides",
                    "min_experts": 1,
                    "max_experts": 3,
                    "independent_first": True,
                    "cross_review": True,
                    "max_review_rounds": 1,
                    "finalizer_role": "host",
                },
                "built_in": True,
                "created_at": now,
                "updated_at": now,
            }
        )
        self.save_template(template)

    def list_templates(self) -> list[RoomTemplate]:
        rows = self.database.conn.execute(
            "SELECT template_json FROM room_templates ORDER BY built_in DESC, updated_at DESC"
        ).fetchall()
        return [RoomTemplate.model_validate(loads(row["template_json"])) for row in rows]

    def get_template(self, template_id: str) -> RoomTemplate | None:
        row = self.database.conn.execute(
            "SELECT template_json FROM room_templates WHERE id = ?", (template_id,)
        ).fetchone()
        if row is None:
            return None
        return RoomTemplate.model_validate(loads(row["template_json"]))

    def save_template(self, template: RoomTemplate) -> RoomTemplate:
        self.database.conn.execute(
            """
            INSERT INTO room_templates (
              id, name, description, protocol, template_json, built_in, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              name = excluded.name,
              description = excluded.description,
              protocol = excluded.protocol,
              template_json = excluded.template_json,
              built_in = excluded.built_in,
              updated_at = excluded.updated_at
            """,
            (
                template.id,
                template.name,
                template.description,
                template.protocol.value,
                dumps(template.model_dump(mode="json")),
                int(template.built_in),
                template.created_at.isoformat(),
                template.updated_at.isoformat(),
            ),
        )
        self.database.conn.commit()
        return template

    def create_template(self, data: dict[str, Any]) -> RoomTemplate:
        now = datetime.now(UTC)
        payload = dict(data)
        payload.setdefault("id", new_id("roomtpl"))
        payload.setdefault("created_at", now)
        payload["updated_at"] = now
        payload["built_in"] = False
        return self.save_template(RoomTemplate.model_validate(payload))

    def update_template(self, template_id: str, patch: dict[str, Any]) -> RoomTemplate:
        current = self.get_template(template_id)
        if current is None:
            raise KeyError(template_id)
        if current.built_in:
            raise ValueError("built_in_template_is_immutable")
        data = current.model_dump(mode="python")
        data.update(patch)
        data["id"] = template_id
        data["built_in"] = False
        data["updated_at"] = datetime.now(UTC)
        return self.save_template(RoomTemplate.model_validate(data))

    def delete_template(self, template_id: str) -> bool:
        current = self.get_template(template_id)
        if current is None:
            return False
        if current.built_in:
            raise ValueError("built_in_template_is_immutable")
        cur = self.database.conn.execute("DELETE FROM room_templates WHERE id = ?", (template_id,))
        self.database.conn.commit()
        return cur.rowcount > 0

    def save_run(
        self,
        *,
        room_id: str,
        protocol: RoomProtocolType,
        state: RoomProtocolState,
    ) -> None:
        if not state.run_id or state.started_at is None:
            raise ValueError("run_identity_required")
        now = datetime.now(UTC).isoformat()
        self.database.conn.execute(
            """
            INSERT INTO room_runs (
              id, room_id, protocol, status, current_stage, state_json,
              started_at, finished_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              status = excluded.status,
              current_stage = excluded.current_stage,
              state_json = excluded.state_json,
              finished_at = excluded.finished_at,
              updated_at = excluded.updated_at
            """,
            (
                state.run_id,
                room_id,
                protocol.value,
                state.status.value,
                state.current_stage,
                dumps(state.model_dump(mode="json")),
                state.started_at.isoformat(),
                state.finished_at.isoformat() if state.finished_at else None,
                now,
            ),
        )
        self.database.conn.commit()

    def save_event(self, event: RoomProtocolEvent) -> None:
        self.database.conn.execute(
            """
            INSERT OR IGNORE INTO room_protocol_events (
              id, room_id, run_id, event_type, stage, actor_id, target_id,
              task_id, status, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event.room_id,
                event.run_id,
                event.event_type,
                event.stage,
                event.actor_id,
                event.target_id,
                event.task_id,
                event.status,
                dumps(event.metadata),
                event.created_at.isoformat(),
            ),
        )
        self.database.conn.commit()

    def save_task(self, room_id: str, task: ProtocolTask) -> None:
        self.database.conn.execute(
            """
            INSERT INTO room_protocol_tasks (
              id, room_id, run_id, parent_task_id, stage, participant_id,
              task_type, status, input_json, output_json, error_type, error,
              created_at, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              status = excluded.status,
              output_json = excluded.output_json,
              error_type = excluded.error_type,
              error = excluded.error,
              started_at = excluded.started_at,
              finished_at = excluded.finished_at
            """,
            (
                task.id,
                room_id,
                task.run_id,
                task.parent_task_id,
                task.stage,
                task.participant_id,
                task.task_type,
                task.status.value,
                dumps(task.input),
                dumps(task.output),
                task.error_type,
                task.error,
                task.created_at.isoformat(),
                task.started_at.isoformat() if task.started_at else None,
                task.finished_at.isoformat() if task.finished_at else None,
            ),
        )
        self.database.conn.commit()

    def latest_run(self, room_id: str) -> RoomProtocolState | None:
        row = self.database.conn.execute(
            "SELECT state_json FROM room_runs WHERE room_id = ? ORDER BY started_at DESC LIMIT 1",
            (room_id,),
        ).fetchone()
        if row is None:
            return None
        return RoomProtocolState.model_validate(loads(row["state_json"]))

    def list_runs(
        self, *, room_id: str | None = None, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM room_runs WHERE 1 = 1"
        params: list[Any] = []
        if room_id:
            sql += " AND room_id = ?"
            params.append(room_id)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(500, int(limit))))
        rows = self.database.conn.execute(sql, params).fetchall()
        return [
            {
                "id": row["id"],
                "room_id": row["room_id"],
                "protocol": row["protocol"],
                "status": row["status"],
                "current_stage": row["current_stage"],
                "state": loads(row["state_json"]),
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        rows = self.database.conn.execute(
            "SELECT * FROM room_runs WHERE id = ?", (run_id,)
        ).fetchall()
        if not rows:
            return None
        row = rows[0]
        tasks = self.database.conn.execute(
            "SELECT * FROM room_protocol_tasks WHERE run_id = ? ORDER BY created_at",
            (run_id,),
        ).fetchall()
        return {
            "id": row["id"],
            "room_id": row["room_id"],
            "protocol": row["protocol"],
            "status": row["status"],
            "current_stage": row["current_stage"],
            "state": loads(row["state_json"]),
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "updated_at": row["updated_at"],
            "tasks": [
                {
                    "id": task["id"],
                    "parent_task_id": task["parent_task_id"],
                    "stage": task["stage"],
                    "participant_id": task["participant_id"],
                    "task_type": task["task_type"],
                    "status": task["status"],
                    "input": loads(task["input_json"]),
                    "output": loads(task["output_json"]),
                    "error_type": task["error_type"],
                    "error": task["error"],
                    "created_at": task["created_at"],
                    "started_at": task["started_at"],
                    "finished_at": task["finished_at"],
                }
                for task in tasks
            ],
        }

    def delete_run(self, run_id: str) -> bool:
        current = self.get_run(run_id)
        if current is None:
            return False
        if not self.terminal(str(current["status"])):
            raise ValueError("active_room_run_must_be_cancelled_before_delete")
        cur = self.database.conn.execute("DELETE FROM room_runs WHERE id = ?", (run_id,))
        self.database.conn.commit()
        return cur.rowcount > 0

    def list_events(self, room_id: str, run_id: str | None = None) -> list[RoomProtocolEvent]:
        sql = "SELECT * FROM room_protocol_events WHERE room_id = ?"
        params: list[Any] = [room_id]
        if run_id:
            sql += " AND run_id = ?"
            params.append(run_id)
        sql += " ORDER BY created_at ASC"
        rows = self.database.conn.execute(sql, params).fetchall()
        return [
            RoomProtocolEvent.model_validate(
                {
                    "id": row["id"],
                    "room_id": row["room_id"],
                    "run_id": row["run_id"],
                    "event_type": row["event_type"],
                    "stage": row["stage"],
                    "actor_id": row["actor_id"],
                    "target_id": row["target_id"],
                    "task_id": row["task_id"],
                    "status": row["status"],
                    "metadata": loads(row["metadata_json"]),
                    "created_at": row["created_at"],
                }
            )
            for row in rows
        ]

    def count_votes(self, votes: list[dict[str, Any]], *, weighted: bool) -> dict[str, float]:
        totals: dict[str, float] = {}
        for vote in votes:
            option = str(vote.get("vote") or "abstain")
            amount = float(vote.get("weight", 1.0)) if weighted else 1.0
            totals[option] = totals.get(option, 0.0) + amount
        return totals

    @staticmethod
    def terminal(status: RoomRunStatus | str) -> bool:
        return RoomRunStatus(status) in {
            RoomRunStatus.SUCCESS,
            RoomRunStatus.PARTIAL_SUCCESS,
            RoomRunStatus.FAILED,
            RoomRunStatus.CANCELLED,
        }
