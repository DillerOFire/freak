from bot.telemetry.audit import audit_telemetry_export


def test_audit_telemetry_export_computes_weighted_cache_and_budget_violations():
    document = {
        "events": [
            {
                "source": "message",
                "prompt_tokens": 100,
                "prompt_cached_tokens": 60,
                "context_prompt": (
                    "<related_research><note>one</note><note>two</note>"
                    "</related_research><saved_media>items</saved_media>"
                ),
                "saved_media_policy": {"mode": "normal"},
                "saved_media_option_count": 12,
                "cache_stable_message_count": 0,
            },
            {
                "source": "message",
                "prompt_tokens": 300,
                "prompt_cached_tokens": 0,
                "context_prompt": "",
                "saved_media_policy": {"mode": "normal"},
                "saved_media_option_count": 4,
                "cache_stable_message_count": 20,
            },
            {
                "source": "ponder_agent",
                "prompt_tokens": 1000,
                "prompt_cached_tokens": 1000,
            },
        ]
    }

    audit = audit_telemetry_export(document)

    assert audit["event_count"] == 2
    assert audit["weighted_cached_share"] == 0.15
    assert audit["cache_hit_call_rate"] == 0.5
    assert audit["normal_media_max_options"] == 12
    assert audit["max_related_research_notes"] == 2
    assert len(audit["violations"]) == 2
