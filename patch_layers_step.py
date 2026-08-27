import re

with open("creative-variants-mvp/frontend/steps/layers_step.py", "r") as f:
    content = f.read()

import_statement = """import streamlit as st
from streamlit_drawable_canvas import st_canvas
import io
from PIL import Image
"""
content = content.replace("import streamlit as st", import_statement)

# Replace the expander "Pincel (añadir o quitar zonas)" block.
pincel_block_start = 'with st.expander("Pincel (añadir o quitar zonas)"):\\n'
pincel_replacement = """    with st.expander("Pincel (dibujo libre a mano alzada)"):
        st.caption("Dibuja sobre la imagen para definir la nueva máscara de la capa.")
        bg_image_path = cached_file(project["project_id"], project["source"]["path"], token())
        bg_image = Image.open(bg_image_path).convert("RGBA")
        
        # Calculate proportional size for display
        display_width = 600
        ratio = display_width / canvas["width"]
        display_height = int(canvas["height"] * ratio)
        
        stroke_width = st.slider("Tamaño del pincel", 1, 50, 10, key=f"sw-{layer['id']}")
        
        canvas_result = st_canvas(
            fill_color="rgba(0, 255, 0, 0.3)",
            stroke_width=stroke_width,
            stroke_color="rgba(0, 255, 0, 1.0)",
            background_image=bg_image,
            update_streamlit=False,
            height=display_height,
            width=display_width,
            drawing_mode="freedraw",
            key=f"canvas-{layer['id']}",
        )
        
        if st.button("Guardar dibujo como máscara", type="primary", key=f"save-canvas-{layer['id']}"):
            if canvas_result.image_data is not None:
                # Resize the canvas mask back to original resolution
                mask_image = Image.fromarray(canvas_result.image_data.astype('uint8'), 'RGBA')
                mask_image = mask_image.resize((canvas["width"], canvas["height"]), Image.NEAREST)
                # Convert to L (grayscale)
                mask_l = Image.new("L", mask_image.size, 0)
                mask_l.paste(255, mask=mask_image.split()[3]) # Use alpha channel as mask
                
                buf = io.BytesIO()
                mask_l.save(buf, format="PNG")
                api.upload_mask(project["project_id"], layer["id"], buf.getvalue())
                refresh_project()
                st.rerun()
"""

# I need to find the correct string to replace
regex = re.compile(r'    with st\.expander\("Pincel \(añadir o quitar zonas\)"\):.*?(?=\n\n\ndef _behaviour)', re.DOTALL)
content = regex.sub(pincel_replacement, content)

with open("creative-variants-mvp/frontend/steps/layers_step.py", "w") as f:
    f.write(content)
