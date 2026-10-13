# Guía del artículo

La escribe el editor del artículo (el responsable, si `roles.yaml` no declara otro).
Cada sesión la lee antes de escribir, para que el artículo se lea como un solo texto.
Si una parte no encaja con lo que dice esta guía, se conversa con el editor en la tarea.
Mantenla corta, con una oración por línea: entra al contexto de cada sesión y no debería pasar de 16 KiB.

## Notación y terminología
- La notación está en `tex/notation.tex`; aquí van solo las convenciones de uso.
- (Lista aquí los términos del proyecto y la forma elegida para cada uno.)

## Tono y extensión
- (Define aquí el tono, la persona gramatical y la extensión aproximada de cada sección.)

## Secciones del artículo
Cada sección es un archivo de `tex/outline/` que incluye sus partes en orden.

### Introducción (`tex/outline/introduction.tex`)
- Qué cubre: el problema, la motivación, las preguntas de investigación y las contribuciones.
- Partes: `introduction`.

### Antecedentes y trabajo previo (`tex/outline/background.tex`)
- Qué cubre: el contexto del problema y el trabajo relacionado, y en qué se distingue este trabajo.
- Partes: `background`.

### Marco teórico (`tex/outline/theory.tex`)
- Qué cubre: los conceptos, las definiciones y los modelos en los que se apoya el trabajo.
- Partes: `theory`.

### Propuesta y metodología (`tex/outline/methodology.tex`)
- Qué cubre: la propuesta, los datos, los procedimientos y las decisiones de diseño.
- Partes: `methodology`.

### Experimentos (`tex/outline/experiments.tex`)
- Qué cubre: el diseño, la configuración y la ejecución de los experimentos.
- Partes: `experiments`.
- Si la escriben varias personas, se divide en subpartes, como `experiments-a` y `experiments-b`, y aquí se dice qué cubre cada una.

### Análisis de resultados (`tex/outline/results.tex`)
- Qué cubre: los resultados de los experimentos, con sus figuras y tablas, y su interpretación.
- Partes: `results`.

### Conclusiones (`tex/outline/conclusions.tex`)
- Qué cubre: las conclusiones, las limitaciones y el trabajo futuro.
- Partes: `conclusions`.
