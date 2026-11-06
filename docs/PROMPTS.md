# Prompts para momentos clave

Los flujos repetibles son comandos (`/run-eval`, `/pre-submit`, `/explore-data`, `/cleanup`, `/handoff`) y skills
(`bankrec-policies`, `new-matching-rule`, `dataset-onboarding`, `leaderboard-loop`).

## Arranque de cada rol
```
Lee CLAUDE.md y la skill bankrec-policies. Mi rol es <A|B|C|D> (docs/PLAYBOOK.md). Ejecuta
`uv run closebooks run train` y, con `closebooks diff`, dime las 3 reglas con más impacto en mis segmentos,
con el nº de líneas que arreglarían. No implementes hasta mi OK; después, skill new-matching-rule.
```

## Un segmento concreto
```
El segmento <seg> está al <x>% en train. Lanza el subagente failure-analyst sobre él, propón la regla y,
cuando te dé el OK, impleméntala con test. Quiero el score de sample y de train antes y después.
```

## LIVE peor de lo esperado
```
El envío LIVE <hora> sacó <y> (train daba <x>). Compara la distribución de reglas de out/live/explain.csv.gz con
la de train y busca drift (skill dataset-onboarding: Moncayo camt.053 desde 2025-10-01, avisos con formato nuevo).
```

## FINAL (18:00)
```
Sigue la skill leaderboard-loop, sección "Protocolo FINAL". Descarga final, verifica el SHA, ejecuta
`closebooks run final`, lanza reconciliation-auditor sobre el resultado y prepárame el comando de envío.
NO envíes tú.
```

## Vídeo y descripción
```
Con CLAUDE.md, docs/SUBMISSIONS.md y out/*/score.json, escribe el guion de un vídeo de 4:30 (problema, enfoque,
demo, resultados, uso de Claude Code) y la descripción del proyecto (ideas clave, stack, siguientes pasos, miembros).
```
