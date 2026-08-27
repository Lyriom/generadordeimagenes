import re

with open('creative-variants-mvp/frontend/steps/simple_step.py', 'r') as f:
    content = f.read()

# Add import time at the top
content = content.replace("import streamlit as st\n", "import time\nimport streamlit as st\n")

# Add polling helper
polling_func = """
def _poll_task(project_id: str, task_id: str, progress_text: str):
    progress_bar = st.progress(0, text=progress_text)
    while True:
        status = api.get_task_status(project_id, task_id)
        if status["state"] == "COMPLETED":
            progress_bar.progress(1.0, text=f"{progress_text} (Completado)")
            time.sleep(0.5)
            progress_bar.empty()
            return status["result"]
        elif status["state"] == "FAILED":
            progress_bar.empty()
            raise Exception("La tarea asíncrona falló.")
        elif status["state"] == "PROGRESS":
            meta = status["meta"] or {}
            progress_bar.progress(meta.get("progress", 0) / 100.0, text=meta.get("status", progress_text))
        time.sleep(2)
"""

content = content.replace("def _generate(projects: list[dict], product_batch: dict, targets: dict[str, str]) -> None:", polling_func + "\ndef _generate(projects: list[dict], product_batch: dict, targets: dict[str, str]) -> None:")

# Replace api.auto_generate with polling
content = content.replace("result = api.auto_generate(", "task = api.auto_generate(")
content = content.replace("regenerate_background=(\n                                background_provider == \"openai\" and first_batch\n                            ),\n                        )", "regenerate_background=(\n                                background_provider == \"openai\" and first_batch\n                            ),\n                        )\n                        result = _poll_task(project[\"project_id\"], task[\"task_id\"], f\"Generando {product.name}...\")")

with open('creative-variants-mvp/frontend/steps/simple_step.py', 'w') as f:
    f.write(content)
