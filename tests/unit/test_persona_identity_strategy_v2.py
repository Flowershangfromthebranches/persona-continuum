from persona_continuum.application.identity_resolver import (
    IdentityResolver,
    ResearchQueryBuilder,
)
from persona_continuum.domain.identity import (
    IdentitySpec,
    LifeStatus,
    PrivacyScope,
    ResearchMode,
    SubjectKind,
    WebResearchScope,
)


def test_fictional_character_resolution_and_queries():
    spec = IdentitySpec(
        display_name="夏彦",
        aliases=["Luke Pearce"],
        subject_kind=SubjectKind.FICTIONAL_CHARACTER,
        work_or_universe="未定事件簿",
        identity_context="《未定事件簿》中的男主角夏彦，HoYoverse 游戏角色，不是现实同名人物",
        research_mode=ResearchMode.WEB,
    )
    assert spec.effective_web_scope() == WebResearchScope.IDENTITY_AND_CANON
    resolved = IdentityResolver.resolve_spec(spec)
    assert resolved.canonical_name == "夏彦"
    assert resolved.work_or_universe == "未定事件簿"
    assert "夏彦 未定事件簿" in resolved.positive_search_terms

    raw_queries = [
        "夏彦 官方设定集 个人背景",
        "夏彦 剧情梳理 时间线",
        "未定事件簿 夏彦 渡鸦特工",
    ]
    built = ResearchQueryBuilder.build_queries(spec, resolved, raw_queries)
    for q in built:
        assert "未定事件簿" in q
        assert "夏彦" in q


def test_original_character_background_only_queries():
    spec = IdentitySpec(
        display_name="林默",
        subject_kind=SubjectKind.ORIGINAL_CHARACTER,
        identity_context="2077年上海刑警",
        research_mode=ResearchMode.WEB,
    )
    assert spec.effective_web_scope() == WebResearchScope.BACKGROUND_ONLY
    resolved = IdentityResolver.resolve_spec(spec)

    raw_queries = [
        "林默 人物经历 履历 档案",
        "林默 刑警 工作方式 破案记录",
    ]
    built = ResearchQueryBuilder.build_queries(spec, resolved, raw_queries)
    # The character's personal name '林默' must NOT be bare-searched
    for q in built:
        assert "林默" not in q or "上海" in q


def test_real_person_public_compatibility():
    spec = IdentitySpec(
        display_name="Elon Musk",
        subject_kind=SubjectKind.REAL_PERSON,
        life_status=LifeStatus.LIVING,
        privacy_scope=PrivacyScope.PUBLIC,
        research_mode=ResearchMode.WEB,
    )
    p_type, mode = spec.to_legacy_pair()
    assert p_type.value == "public_living_person"
    assert mode == "public_research"


def test_real_person_deceased_compatibility():
    spec = IdentitySpec(
        display_name="Steve Jobs",
        subject_kind=SubjectKind.REAL_PERSON,
        life_status=LifeStatus.DECEASED,
        privacy_scope=PrivacyScope.PUBLIC,
        research_mode=ResearchMode.WEB,
    )
    p_type, mode = spec.to_legacy_pair()
    assert p_type.value == "public_historical_person"
    assert mode == "public_research"


def test_fictional_character_legacy_mapping_allows_public_research():
    spec = IdentitySpec(
        display_name="夏彦",
        subject_kind=SubjectKind.FICTIONAL_CHARACTER,
        work_or_universe="未定事件簿",
        research_mode=ResearchMode.WEB,
    )
    p_type, mode = spec.to_legacy_pair()
    assert p_type.value == "fictional_or_synthetic_person"
    assert mode == "public_research"


def test_from_legacy_mapping():
    spec1 = IdentitySpec.from_legacy(
        display_name="Steve Jobs",
        persona_type="public_historical_person",
        creation_mode="public_research",
    )
    assert spec1.subject_kind == SubjectKind.REAL_PERSON
    assert spec1.life_status == LifeStatus.DECEASED
    assert spec1.privacy_scope == PrivacyScope.PUBLIC

    spec2 = IdentitySpec.from_legacy(
        display_name="夏彦",
        persona_type="fictional_or_synthetic_person",
        creation_mode="public_research",
        extra={"work_or_universe": "未定事件簿"},
    )
    assert spec2.subject_kind == SubjectKind.FICTIONAL_CHARACTER
    assert spec2.work_or_universe == "未定事件簿"

