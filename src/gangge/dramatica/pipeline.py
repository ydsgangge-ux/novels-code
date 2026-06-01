"""
写作管线
修复：
- AuditIssue 构造不再传 excerpt 到 location
- 章后调用 update_current_state_md
- 集成 SummaryAgent，写完生成摘要注入 chapter_summaries.md
- WriterAgent 获得前情摘要上下文
- _apply_settlement 完整处理位置/情感/关系变化
多线叙事扩展：
- 多线程调度：根据章纲的 thread_id 解析视角角色和线程上下文
- 跨线程感知：建筑师/写手/审计员均获得其他线程状态
- 时间轴记录：写完章节后自动添加 TimelineEvent
- 线程权重管理：根据线程的 weight 调整字数分配
- 支线掉线预警：章后更新 thread_status.md
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict

from .agents import (
    ArchitectAgent, ArchitectBlueprint,
    WriterAgent, WriterOutput,
    AuditorAgent, AuditReport, AuditIssue,
    ReviserAgent, ReviseResult,
    SummaryAgent,
    PreWriteChecklist,
)
from .narrative import NarrativeEngine, ChapterOutlineSchema
from .state import StateManager
from .types.narrative import Character, NarrativeThread, TimelineEvent
from .types.state import (
    TruthFileKey, EmotionalSnapshot, CausalLink, AffectedDecision,
    Hook, HookType, HookStatus,
)
from .validators import PostWriteValidator

_GRAPH_AVAILABLE = False
try:
    from .narrative_graph import NarrativeGraphDB, NarrativeIndexer, ConsistencyChecker
    _GRAPH_AVAILABLE = True
except ImportError:
    pass


@dataclass
class PipelineResult:
    chapter_number: int
    content: str
    audit_report: AuditReport
    validation_passed: bool
    revision_rounds: int
    causal_links: int
    word_count: int
    thread_id: str = ""
    pov_character_id: str = ""
    dormancy_warnings: list[str] = field(default_factory=list)
    graph_indexed: bool = False
    consistency_issues: list[dict] = field(default_factory=list)


class WritingPipeline:
    """
    单章写作管线：

    快照备份
        ↓
    [建筑师] 规划蓝图（含前情摘要上下文）
        ↓
    [写手] 生成正文 + 写后结算表
        ↓
    [写后验证器] 零 LLM 硬规则检测
        ↓ error → spot-fix
    [审计员] 叙事质量审计（temperature=0）
        ↓ critical → 修订 → 再审（最多 MAX_REVISE_ROUNDS 轮）
    保存最终稿
        ↓
    [因果链提取] 从正文中提取因果关系
        ↓
    [摘要生成] 生成章节摘要 → chapter_summaries.md
        ↓
    [状态更新] 结算表 → world_state.json + current_state.md
    """

    MAX_REVISE_ROUNDS = 2

    def __init__(
        self,
        state_manager: StateManager,
        architect: ArchitectAgent,
        writer: WriterAgent,
        auditor: AuditorAgent,
        reviser: ReviserAgent,
        narrative_engine: NarrativeEngine,
        summary_agent: SummaryAgent,
        validator: PostWriteValidator,
        protagonist: Character,
        all_characters: list[Character],
    ):
        self.sm = state_manager
        self.architect = architect
        self.writer = writer
        self.auditor = auditor
        self.reviser = reviser
        self.engine = narrative_engine
        self.summary_agent = summary_agent
        self.validator = validator
        self.protagonist = protagonist
        self.all_characters = all_characters

    def run(
        self,
        chapter_outline: ChapterOutlineSchema,
        verbose: bool = False,
        fast_mode: bool = False,
    ) -> PipelineResult:
        ch = chapter_outline.chapter_number
        title = chapter_outline.title

        def log(msg: str) -> None:
            if verbose:
                print(f"  [{ch}] {msg}")

        # ── 快照 ─────────────────────────────────────────────────────────────
        log("创建快照...")
        self.sm.create_snapshot(ch - 1)

        # ── 读取上下文 ────────────────────────────────────────────────────────
        world_context = self.sm.read_truth_bundle([
            TruthFileKey.CURRENT_STATE,
            TruthFileKey.CHARACTER_MATRIX,
        ])
        pending_hooks = self.sm.read_truth(TruthFileKey.PENDING_HOOKS)

        # 因果链 + 情感弧线（写手也需要看到）
        causal_chain = self.sm.read_truth(TruthFileKey.CAUSAL_CHAIN)
        emotional_arcs = self.sm.read_truth(TruthFileKey.EMOTIONAL_ARCS)

        # 前情摘要：取最近 3 章
        full_summaries = self.sm.read_truth(TruthFileKey.CHAPTER_SUMMARIES)
        prior_summaries = _extract_recent_summaries(full_summaries, n=3)

        # ── 多线程上下文解析 ──────────────────────────────────────────────────
        ws = self.sm.read_world_state()
        thread_id = getattr(chapter_outline, "thread_id", "thread_main") or "thread_main"
        pov_char_id = getattr(chapter_outline, "pov_character_id", "") or ""

        # 解析视角角色
        pov_character: Character | None = None
        if pov_char_id:
            for c in self.all_characters:
                if c.id == pov_char_id:
                    pov_character = c
                    break
        # 回退到线程的 pov_character_id
        if not pov_character and thread_id:
            thread = ws.get_thread(thread_id)
            if thread and thread.pov_character_id:
                for c in self.all_characters:
                    if c.id == thread.pov_character_id:
                        pov_character = c
                        break

        # 构建跨线程上下文摘要
        thread_context = self._build_thread_context(ws, thread_id, ch)

        # 根据线程权重调整字数
        effective_thread = ws.get_thread(thread_id) if thread_id else None
        thread_weight = effective_thread.weight if effective_thread else 1.0
        adjusted_target_words = max(
            int(chapter_outline.target_words * thread_weight),
            int(chapter_outline.target_words * 0.5),
        )

        if verbose and effective_thread:
            log(f"线程：{effective_thread.name}（{thread_id}），权重={thread_weight}，调整字数={adjusted_target_words}")

        # ── 1. 建筑师规划 ─────────────────────────────────────────────────────
        if fast_mode:
            log("快速模式：轻量蓝图推导...")
            blueprint = self._lightweight_blueprint(
                chapter_outline, world_context, pending_hooks,
                pov_character, thread_id, thread_context,
            )
        else:
            log("建筑师规划...")
            blueprint = self.architect.plan_chapter(
                chapter_outline=chapter_outline,
                world_context=world_context,
                pending_hooks=pending_hooks,
                prior_chapter_summary=prior_summaries,
                pov_character=pov_character,
                thread_context=thread_context,
            )

        # ── 2. 写手写章 ───────────────────────────────────────────────────────
        log("写手写章...")
        scene_summaries = _format_beats(chapter_outline)
        writer_output = self.writer.write_chapter(
            scene_summaries=scene_summaries,
            blueprint=blueprint,
            protagonist=self.protagonist,
            world_context=world_context,
            chapter_number=ch,
            target_words=adjusted_target_words,
            prior_summaries=prior_summaries,
            chapter_title=title,
            pov_character=pov_character,
            thread_context=thread_context,
            pending_hooks=pending_hooks,
            causal_chain=causal_chain,
            emotional_arcs=emotional_arcs,
            writing_notes=chapter_outline.writing_notes,
            pov_instruction=chapter_outline.pov,
        )
        self.sm.save_draft(ch, writer_output.content)
        log(f"草稿 {len(writer_output.content)} 字")

        # ── 3. 写后验证（零 LLM） ────────────────────────────────────────────
        log("写后验证...")
        val_result = self.validator.validate(
            writer_output.content,
            target_words=adjusted_target_words,
        )
        current_content = writer_output.content

        if not val_result.passed and not fast_mode:
            error_issues = [
                AuditIssue(
                    dimension="写后验证",
                    severity="critical",
                    description=i.description,
                    location=i.excerpt,
                )
                for i in val_result.issues
                if i.severity == "error"
            ]
            log(f"验证未通过（{len(error_issues)} 个 error），spot-fix...")
            fix_result = self.reviser.revise(current_content, error_issues, mode="spot-fix")
            current_content = fix_result.content

        # ── 4. 审计 → 修订闭环 ────────────────────────────────────────────────
        revision_rounds = 0
        DEEP_AUDIT_INTERVAL = 10
        need_deep_audit = (not fast_mode) or (ch % DEEP_AUDIT_INTERVAL == 0)

        if not need_deep_audit:
            log("快速模式：跳过审计修订...")
            audit_report = AuditReport(passed=True, issues=[], critical_count=0)
        else:
            if fast_mode:
                log(f"快速模式周期性深度审查（每{DEEP_AUDIT_INTERVAL}章）...")
            else:
                log("审计员审计...")
            audit_truth_ctx = self.sm.read_truth_bundle([
                TruthFileKey.CURRENT_STATE,
                TruthFileKey.CHARACTER_MATRIX,
                TruthFileKey.PENDING_HOOKS,
                TruthFileKey.EMOTIONAL_ARCS,
                TruthFileKey.CAUSAL_CHAIN,
            ])

            cross_thread_audit_ctx = self._build_cross_thread_audit_context(ws, thread_id, ch)

            audit_report = self.auditor.audit_chapter(
                chapter_content=current_content,
                chapter_number=ch,
                blueprint=blueprint,
                truth_context=audit_truth_ctx,
                settlement=writer_output.settlement,
                cross_thread_context=cross_thread_audit_ctx,
            )

            while not audit_report.passed and revision_rounds < self.MAX_REVISE_ROUNDS:
                log(f"修订第 {revision_rounds + 1} 轮（{audit_report.critical_count} critical）...")
                revise_result = self.reviser.revise(
                    current_content,
                    audit_report.issues,
                    mode="spot-fix",
                )
                current_content = revise_result.content
                revision_rounds += 1

                audit_report = self.auditor.audit_chapter(
                    chapter_content=current_content,
                    chapter_number=ch,
                    blueprint=blueprint,
                    truth_context=audit_truth_ctx,
                    settlement=writer_output.settlement,
                    cross_thread_context=cross_thread_audit_ctx,
                )

        # ── 5. 保存最终稿 ─────────────────────────────────────────────────────
        self.sm.save_final(ch, current_content)
        log(f"最终稿保存（{len(current_content)} 字）")

        # ── 6+7. 因果链提取 + 摘要生成（并行） ─────────────────────────────────
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _extract_causal():
            log("提取因果链...")
            return self.engine.extract_causal_links(
                chapter_content=current_content,
                chapter_number=ch,
                characters=self.all_characters,
            )

        def _generate_summary():
            log("生成章节摘要...")
            return self.summary_agent.generate_summary(
                chapter_content=current_content,
                chapter_number=ch,
                chapter_title=title,
                settlement=writer_output.settlement,
            )

        causal_schemas = []
        summary = None
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                pool.submit(_extract_causal): "causal",
                pool.submit(_generate_summary): "summary",
            }
            for future in as_completed(futures):
                label = futures[future]
                try:
                    if label == "causal":
                        causal_schemas = future.result()
                    else:
                        summary = future.result()
                except Exception as e:
                    log(f"{label} 提取失败: {e}")

        for link_schema in causal_schemas:
            cl = CausalLink(
                id=link_schema.id,
                chapter=link_schema.chapter,
                cause=link_schema.cause,
                event=link_schema.event,
                consequence=link_schema.consequence,
                affected_decisions=[
                    AffectedDecision(d.character_id, d.decision)
                    for d in link_schema.affected_decisions
                ],
                triggered_events=link_schema.triggered_events,
                thread_id=thread_id,
            )
            self.sm.add_causal_link(cl)
        log(f"因果链：{len(causal_schemas)} 条")

        if summary:
            try:
                summary_md = self.summary_agent.format_for_truth_file(summary)
                self.sm.append_truth(TruthFileKey.CHAPTER_SUMMARIES, summary_md)
            except Exception as e:
                log(f"摘要格式化失败（{e}），使用 fallback")
                fallback = (
                    f"\n## 第 {ch} 章《{title}》\n"
                    f"{chapter_outline.summary}\n"
                    f"- 审计：{'通过' if audit_report.passed else '未通过'}"
                    f"，修订 {revision_rounds} 轮\n---\n"
                )
                self.sm.append_truth(TruthFileKey.CHAPTER_SUMMARIES, fallback)
        else:
            fallback = (
                f"\n## 第 {ch} 章《{title}》\n"
                f"{chapter_outline.summary}\n"
                f"- 审计：{'通过' if audit_report.passed else '未通过'}"
                f"，修订 {revision_rounds} 轮\n---\n"
            )
            self.sm.append_truth(TruthFileKey.CHAPTER_SUMMARIES, fallback)
            log("摘要生成失败，使用 fallback")

        # ── 8. 应用结算表到世界状态 ───────────────────────────────────────────
        log("应用结算表...")
        self._apply_settlement(ch, writer_output, blueprint)

        # ── 9. 记录时间轴事件 + 更新线程状态 ─────────────────────────────────
        log("更新时间轴和线程状态...")
        self._record_timeline_events(ch, writer_output, blueprint, thread_id, ws)

        # ── 10. 更新 current_chapter + current_state.md ───────────────────────
        ws = self.sm.read_world_state()
        ws.current_chapter = ch
        self.sm.write_world_state(ws)
        self.sm.update_current_state_md()
        log("current_state.md 已更新")

        # ── 11. 更新线程状态 + 掉线预警 ───────────────────────────────────────
        dormancy_warnings: list[str] = []
        if ws.threads:
            self.sm.update_thread_status_md()
            dormant = ws.dormant_threads(ch, threshold=5)
            for t in dormant:
                gap = ch - t.last_active_chapter
                dormancy_warnings.append(f"{t.name}（{t.id}）：已 {gap} 章未活跃")
            if dormancy_warnings and verbose:
                for w in dormancy_warnings:
                    print(f"  [预警] 支线掉线：{w}")

        # ── 12. 叙事图谱索引 + 一致性检查 ────────────────────────────────────
        graph_indexed = False
        consistency_issues: list[dict] = []
        if _GRAPH_AVAILABLE:
            try:
                graph_db = NarrativeGraphDB(self.sm.state_dir / "narrative_graph.db")
                graph_db.initialize()
                indexer = NarrativeIndexer(graph_db)

                ws_dict = asdict(ws)
                indexer.index_world_state(ws_dict)

                known_chars = {c.id: c.name for c in self.all_characters}
                indexer.index_chapter(ch, title, current_content, known_chars)

                checker = ConsistencyChecker(graph_db)
                issues = checker.check_all(ch)
                consistency_issues = [
                    {
                        "severity": i.severity.value,
                        "category": i.category.value,
                        "description": i.description,
                        "chapter": i.chapter,
                        "suggestion": i.suggestion,
                    }
                    for i in issues
                ]
                graph_db.close()
                graph_indexed = True
                if verbose:
                    log(f"图谱索引完成，一致性检查发现 {len(issues)} 个问题")
            except Exception as e:
                if verbose:
                    log(f"图谱索引失败（{e}），不影响写作结果")

        # ── 13. 长篇上下文自动压缩 ──────────────────────────────────────────
        try:
            self.sm.auto_compact(ch)
        except Exception:
            pass

        return PipelineResult(
            chapter_number=ch,
            content=current_content,
            audit_report=audit_report,
            validation_passed=val_result.passed,
            revision_rounds=revision_rounds,
            causal_links=len(causal_schemas),
            word_count=len(current_content),
            thread_id=thread_id,
            pov_character_id=pov_character.id if pov_character else "",
            dormancy_warnings=dormancy_warnings,
            graph_indexed=graph_indexed,
            consistency_issues=consistency_issues,
        )

    # ── 轻量蓝图推导（快速模式，零 LLM 调用） ────────────────────────────────

    def _lightweight_blueprint(
        self,
        chapter_outline: ChapterOutlineSchema,
        world_context: str,
        pending_hooks: str,
        pov_character: Character | None,
        thread_id: str,
        thread_context: str,
    ) -> ArchitectBlueprint:
        import re as _re

        core_conflict = chapter_outline.summary if chapter_outline.summary else "本章核心冲突待定"

        mandatory = chapter_outline.mandatory_tasks or []
        if mandatory:
            core_conflict = mandatory[0]

        hooks_to_advance = []
        if pending_hooks and pending_hooks.strip():
            hook_lines = [l.strip() for l in pending_hooks.split("\n") if l.strip() and not l.startswith("#")]
            for hl in hook_lines[:5]:
                hid_match = _re.search(r'`?(\w{3,})`?', hl)
                if hid_match:
                    hooks_to_advance.append(hid_match.group(1))

        hooks_to_plant = []
        if chapter_outline.end_hook:
            hooks_to_plant.append(chapter_outline.end_hook)

        emotional_arc = chapter_outline.emotional_arc or {}
        emotional_journey = {
            "start": emotional_arc.get("start", "平静"),
            "end": emotional_arc.get("end", "紧张"),
        }

        chapter_end_hook = chapter_outline.end_hook or "留下悬念，驱动读者继续阅读"

        beats = chapter_outline.beats or []
        fast_beats = sum(1 for b in beats if any(kw in b.description for kw in ["冲突", "对抗", "危机", "高潮", "揭示", "转折"]))
        slow_beats = len(beats) - fast_beats
        if fast_beats > slow_beats:
            pace_notes = "快节奏为主，紧张场景密集，注意穿插短暂喘息"
        elif slow_beats > fast_beats:
            pace_notes = "慢节奏铺垫为主，注意在关键节点制造张力"
        else:
            pace_notes = "张弛有度，快慢场景交替"

        active_chars = []
        if pov_character:
            active_chars.append(pov_character.name)
        for c in self.all_characters:
            if c.id != (pov_character.id if pov_character else ""):
                if any(c.name in (b.description or "") for b in beats):
                    active_chars.append(c.name)
        if len(active_chars) < 2 and len(self.all_characters) > 1:
            for c in self.all_characters[:3]:
                if c.name not in active_chars:
                    active_chars.append(c.name)

        risk_lines = []
        if hooks_to_advance:
            risk_lines.append(f"需推进 {len(hooks_to_advance)} 条伏笔，注意自然融入而非生硬提及")
        if len(beats) > 5:
            risk_lines.append("节拍较多，注意场景间过渡自然")
        if world_context and len(world_context) > 2000:
            risk_lines.append("世界状态复杂，注意角色位置和情感状态一致性")
        risk_scan = "；".join(risk_lines) if risk_lines else "无特别高风险点"

        return ArchitectBlueprint(
            core_conflict=core_conflict,
            hooks_to_advance=hooks_to_advance,
            hooks_to_plant=hooks_to_plant,
            emotional_journey=emotional_journey,
            chapter_end_hook=chapter_end_hook,
            pace_notes=pace_notes,
            pre_write_checklist=PreWriteChecklist(
                active_characters=active_chars,
                required_locations=[],
                resources_in_play=[],
                hooks_status=[f"待推进: {h}" for h in hooks_to_advance],
                risk_scan=risk_scan,
            ),
            pov_character_id=pov_character.id if pov_character else "",
            thread_id=thread_id,
            thread_context=thread_context,
        )

    # ── 多线程辅助方法 ────────────────────────────────────────────────────────

    def _build_thread_context(
        self, ws, current_thread_id: str, chapter: int,
    ) -> str:
        """构建跨线程感知上下文（供建筑师和写手使用）"""
        if not ws.threads:
            return ""

        lines = []
        for t in ws.get_active_threads():
            if t.id == current_thread_id:
                continue
            # 线程基本信息
            lines.append(
                f"- {t.name}（{t.id}）：上次活跃 Ch.{t.last_active_chapter}，"
                f"期待感 {t.hook_score}/100"
            )
            if t.end_hook:
                lines.append(f"  当前悬念：{t.end_hook}")
            if t.goal:
                lines.append(f"  目标：{t.goal}")

        if not lines:
            return ""
        return "\n".join(lines)

    def _build_cross_thread_audit_context(
        self, ws, current_thread_id: str, chapter: int,
    ) -> str:
        """构建跨线程审计上下文（供审计员检测跨线程冲突）"""
        parts = []

        # 其他线程的近期时间轴
        for t in ws.threads:
            if t.id == current_thread_id:
                continue
            thread_events = [e for e in ws.timeline if e.thread_id == t.id and e.chapter <= chapter]
            if thread_events:
                parts.append(f"### {t.name}（{t.id}）近期事件\n")
                for te in thread_events[-5:]:
                    parts.append(
                        f"- Ch.{te.chapter} {te.physical_time}：{te.character_id} 在 {te.location_id} "
                        f"做了 {te.action[:40]}"
                    )
                parts.append("")

        # 跨线程因果链
        cross_links = [cl for cl in ws.causal_chain
                       if cl.source_thread_id and cl.source_thread_id != cl.thread_id
                       and cl.thread_id == current_thread_id]
        if cross_links:
            parts.append("### 受其他线程影响的因果链\n")
            for cl in cross_links[-5:]:
                parts.append(f"- Ch.{cl.chapter}：{cl.event}（因：来自 {cl.source_thread_id} 的 {cl.cause}）")
            parts.append("")

        return "\n".join(parts)

    def _record_timeline_events(
        self,
        chapter: int,
        writer_output: WriterOutput,
        blueprint: ArchitectBlueprint,
        thread_id: str,
        ws,
    ) -> None:
        """根据结算表和蓝图记录时间轴事件"""
        time_order = float(chapter)
        counter = 0

        def _next_order():
            nonlocal counter
            counter += 1
            return time_order + counter * 0.1

        # 从角色位置变化中提取时间轴事件
        for change in writer_output.settlement.character_position_changes:
            char_id = change.get("character_id", "")
            loc_id = change.get("location_id", "")
            if char_id and loc_id:
                event = TimelineEvent(
                    id=f"te_{uuid.uuid4().hex[:8]}",
                    chapter=chapter,
                    physical_time="",
                    time_order=_next_order(),
                    character_id=char_id,
                    location_id=loc_id,
                    action=f"移动到 {loc_id}",
                    thread_id=thread_id,
                )
                self.sm.add_timeline_event(event)

        # 从情感变化中提取关键情感转折事件（仅高强度的）
        for ec in writer_output.settlement.emotional_changes:
            char_id = ec.get("character_id", "")
            intensity = int(ec.get("intensity", 0))
            emotion = ec.get("emotion", "")
            trigger = ec.get("trigger", "")
            if char_id and intensity >= 7:
                event = TimelineEvent(
                    id=f"te_{uuid.uuid4().hex[:8]}",
                    chapter=chapter,
                    physical_time="",
                    time_order=_next_order(),
                    character_id=char_id,
                    action=f"情感转折：{emotion}（强度{intensity}/10），触发：{trigger[:30]}",
                    thread_id=thread_id,
                )
                self.sm.add_timeline_event(event)

        # 从信息揭示中提取事件
        for info in writer_output.settlement.info_revealed:
            char_id = info.get("character_id", "")
            info_key = info.get("info_key", "")
            if char_id and info_key:
                event = TimelineEvent(
                    id=f"te_{uuid.uuid4().hex[:8]}",
                    chapter=chapter,
                    physical_time="",
                    time_order=_next_order(),
                    character_id=char_id,
                    action=f"得知：{info_key}",
                    thread_id=thread_id,
                )
                self.sm.add_timeline_event(event)

        # 从核心冲突中提取主线事件
        if blueprint.core_conflict:
            pov_id = blueprint.pov_character_id or self.protagonist.id
            event = TimelineEvent(
                id=f"te_{uuid.uuid4().hex[:8]}",
                chapter=chapter,
                physical_time="",
                time_order=_next_order(),
                character_id=pov_id,
                action=blueprint.core_conflict[:60],
                thread_id=thread_id,
            )
            self.sm.add_timeline_event(event)

    # ── 结算表应用 ────────────────────────────────────────────────────────────

    def _apply_settlement(
        self,
        chapter: int,
        writer_output: WriterOutput,
        blueprint: ArchitectBlueprint,
    ) -> None:
        s = writer_output.settlement

        # 角色位置变化
        for change in s.character_position_changes:
            char_id = change.get("character_id", "")
            loc_id  = change.get("location_id", "")
            if char_id and loc_id:
                self.sm.move_character(char_id, loc_id)

        # 情感变化
        for ec in s.emotional_changes:
            char_id = ec.get("character_id", "")
            if not char_id:
                continue
            snap = EmotionalSnapshot(
                character_id=char_id,
                emotion=ec.get("emotion", "未知"),
                intensity=int(ec.get("intensity", 5)),
                chapter=chapter,
                trigger=ec.get("trigger", ""),
            )
            self.sm.record_emotion(snap)
            # 更新 emotional_arcs.md
            self.sm.append_truth(
                TruthFileKey.EMOTIONAL_ARCS,
                f"- Ch.{chapter} [{char_id}] {snap.emotion}（{snap.intensity}/10）：{snap.trigger}\n",
            )

        # 关系变化（格式：「角色A-角色B：delta，原因」）
        for rel_str in s.relationship_changes:
            try:
                # 解析 "林尘-慕雪：+20，慕雪开始动摇"
                parts = rel_str.split("：", 1)
                if len(parts) == 2:
                    chars_part = parts[0].strip()
                    detail = parts[1].strip()
                    chars = chars_part.split("-", 1)
                    if len(chars) == 2:
                        char_a = chars[0].strip()
                        char_b_detail = chars[1].strip()
                        # 从 detail 提取 delta
                        import re
                        m = re.search(r'([+-]\d+)', detail)
                        delta = int(m.group(1)) if m else 0
                        reason = re.sub(r'[+-]\d+[，,]?\s*', '', detail).strip()
                        self.sm.update_relationship(char_a, char_b_detail, delta, chapter, reason)
            except Exception:
                pass  # 关系变化解析失败静默跳过

        # 新开伏笔
        for hook_desc in s.new_hooks:
            hook = Hook(
                id=f"hook_{uuid.uuid4().hex[:8]}",
                type=HookType.FORESHADOW,
                description=hook_desc,
                planted_in_chapter=chapter,
                expected_resolution_range=(chapter + 3, chapter + 25),
                status=HookStatus.OPEN,
            )
            self.sm.open_hook(hook)

        # 建筑师计划埋下的伏笔（blueprint.hooks_to_plant）
        for hook_desc in blueprint.hooks_to_plant:
            if hook_desc and hook_desc not in s.new_hooks:
                hook = Hook(
                    id=f"hook_{uuid.uuid4().hex[:8]}",
                    type=HookType.FORESHADOW,
                    description=hook_desc,
                    planted_in_chapter=chapter,
                    expected_resolution_range=(chapter + 5, chapter + 30),
                    status=HookStatus.OPEN,
                )
                self.sm.open_hook(hook)

        # 回收伏笔
        for hook_id in s.resolved_hooks:
            self.sm.resolve_hook(hook_id, chapter)

        # 信息揭示
        for info in s.info_revealed:
            char_id  = info.get("character_id", "")
            info_key = info.get("info_key", "")
            content  = info.get("content", "")
            if char_id and info_key:
                self.sm.learn_info(char_id, info_key, content, chapter, "witnessed")
                # 更新 character_matrix.md
                self.sm.append_truth(
                    TruthFileKey.CHARACTER_MATRIX,
                    f"\n- Ch.{chapter} [{char_id}] 得知：{info_key} — {content}\n",
                )

        # 资源变化写入 current_state（附加记录）
        if s.resource_changes:
            changes_str = "；".join(s.resource_changes)
            self.sm.append_truth(
                TruthFileKey.CURRENT_STATE,
                f"\n### Ch.{chapter} 资源变化\n{changes_str}\n",
            )


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _format_beats(chapter_outline: ChapterOutlineSchema) -> str:
    """将章纲节拍格式化为写手可读的场景序列"""
    lines = []
    for i, b in enumerate(chapter_outline.beats):
        fn_label = f"【{b.dramatic_function.value}】"
        line = f"节拍{i+1}{fn_label}：{b.description}"
        if b.emotional_target:
            line += f"（情感目标：{b.emotional_target}）"
        if b.target_words:
            line += f"（约 {b.target_words} 字）"
        lines.append(line)
        if b.detail:
            lines.append(f"  写作指导：{b.detail}")
    return "\n".join(lines) if lines else "（无节拍信息，根据章节摘要自由发挥）"


def _extract_recent_summaries(full_summaries: str, n: int = 3) -> str:
    """从 chapter_summaries.md 中提取最近 n 章的摘要"""
    if not full_summaries.strip():
        return ""
    # 按 "## 第X章" 分割
    import re
    sections = re.split(r'\n(?=## 第\d+章)', full_summaries)
    recent = [s for s in sections if s.strip().startswith("## 第")]
    if not recent:
        return ""
    return "\n".join(recent[-n:])
