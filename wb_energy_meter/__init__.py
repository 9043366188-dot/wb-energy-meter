__version__ = "0.25.0"
__app_name__ = "wb-energy-meter"

# Поколение домена, которое ПОНИМАЕТ этот код (docs/migration-plan-v2.md §7,
# wb_energy_meter/domain_generation.py::CODE_GENERATION — держать в синхроне
# вручную (тест test_step62 проверяет): это статическое объявление кода, а
# CURRENT_GENERATION — то, что реально пишется в БД при первой v2-записи;
# 3 — код понимает пофазные привязки, см. mark_phase_binding).
# scripts/self-update.sh (check_rollback_generation) grep'ает это значение
# из $ROLLBACK_DIR/wb_energy_meter/__init__.py тем же способом, каким уже
# грепает __version__ (syntax_check_new_code) — НЕ переименовывать без
# правки self-update.sh.
__code_generation__ = 3
