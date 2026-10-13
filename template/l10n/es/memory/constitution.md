# Constitución del proyecto

## 1. Propósito de este documento
Este documento reúne los principios, el estilo y las reglas de trabajo del proyecto.
La dirección de la investigación (preguntas, hipótesis, alcance y métodos) está en [plan.md](../plan.md), no aquí.
Lo mantiene el responsable y cambia solo con una tarea propia.

## 2. Integridad y ética
- No se escriben datos `restricted` ni `local_only` en archivos, commits, Issues, PR ni notas.
- Los datos crudos no se versionan: `data/README.md` solo los describe y dice cómo obtenerlos.
- Cada afirmación empírica se apoya en una fuente citada o en un análisis reproducible del repositorio.
- Si el proyecto trabaja con personas, se respeta el dictamen de ética y el consentimiento informado.
- Acuerdo de autoría y de apertura, que se guarda fuera del repositorio: firmado el (AAAA-MM-DD), con SHA-256 (pega aquí la huella del archivo firmado).

## 3. Estilo de escritura
- Variante de idioma: (define aquí la variante, por ejemplo español de México).
- Persona y tiempo verbal: (por ejemplo, primera persona del plural y presente para los resultados).
- Terminología: un término por concepto; los términos técnicos se definen en su primera aparición.
- Unidades y números: Sistema Internacional; (define aquí el separador decimal y el de miles).
- Una oración por línea en los archivos `.tex`, en los `.md` de las partes y en los de `memory/`.

## 4. Principios de notación
- La notación del documento está en `tex/notation.tex` y solo allí.
- Un símbolo tiene un solo significado en todo el documento.
- Una macro nueva se propone en el PR y la agrega el editor del artículo.

## 5. Uso de IA
- Cada persona revisa todo lo que produce con IA y se hace responsable de ello.
- Cada PR declara el uso de IA en su sección correspondiente y en su bloque final.
- La IA no figura como autora ni aparece en la lista de créditos.
- (Enlaza aquí la política de uso de IA de tu institución o de la revista de destino.)

## 6. Criterios de revisión
- El cambio responde a la tarea y a los IDs del plan que cita.
- El texto es claro, sigue el estilo de la sección 3 y cita sus fuentes.
- Las figuras y las tablas tienen su procedencia registrada y se pueden regenerar.
- Quien revisa comprueba que el PDF compila y que no quedan citas ni referencias sin resolver.

## 7. Fuentes de verdad
- Dirección: [plan.md](../plan.md).
- Personas, roles y dueños de cada parte: [roles.yaml](../roles.yaml).
- Notación: [tex/notation.tex](../tex/notation.tex).
- Qué cubre cada sección del artículo y cada parte: [tex/guide.md](../tex/guide.md).
- Reglas para agentes de IA: [AGENTS.md](../AGENTS.md).
- Decisiones: [memory/decisions/](decisions/).
