Ticket T-025 : cockpit auto : arrêt sur budget (--max-cost-eur).
Contrat : should_stop(..., spent_eur=0.0, max_cost_eur=None) renvoie « budget » si max_cost_eur n'est pas None et spent_eur >= max_cost_eur (testé APRÈS max_tickets et escalades). cmd_auto : option --max-cost-eur (défaut 0 = pas de limite), dépense = SUM(cost_eur) des runs du projet depuis le démarrage, vérifiée après chaque lot, AUTO_STOP « budget ».
Critères : should_stop et cmd_auto respectent le contrat ; priorités des autres raisons inchangées ; les tests décrits (seuil, None, priorités, cmd_auto s'arrête avec --max-cost-eur 0.25 après un run à 0.30 €) passent.
Périmètre : core/pipeline.py, core/scheduler.py, tests/test_pipeline.py, tests/test_scheduler.py.
