def test_runtime_patch_keeps_canonical_manual_confirmation():
    import runtime_patch
    import universal_runtime

    assert runtime_patch.runtime.confirm_manual_booking is universal_runtime.confirm_manual_booking
