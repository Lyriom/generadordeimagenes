import re

with open("creative-variants-mvp/backend/app/api/projects.py", "r") as f:
    content = f.read()

new_endpoint = """
@router.post(
    "/{project_id}/layers/{layer_id}/mask/upload",
    response_model=Layer,
    summary="Subir una máscara dibujada a mano alzada",
)
async def upload_mask(project_id: str, layer_id: str, mask_file: UploadFile = File(...)):
    project = load_project_or_404(project_id)
    layer = project.layer_by_id(layer_id)
    if not layer:
        raise bad_request("Layer not found")
        
    temp_path = await _stream_upload(project.project_id, mask_file, 10 * 1024 * 1024)
    try:
        import numpy as np
        from PIL import Image
        img = Image.open(temp_path).convert("L")
        mask = np.array(img)
        
        layer_extraction.write_mask(project, layer, mask)
        layer.meta["mask_edited"] = True
        layer.extracted = False
        if layer.category != LayerCategory.BACKGROUND:
            ok, warning = layer_extraction.extract_layer(project, layer, feather=0, force=True)
            if not ok and warning:
                layer.warnings.append(warning)
        storage.save_project(project)
    finally:
        temp_path.unlink(missing_ok=True)
    return layer
"""

content = content.replace("def edit_mask(project_id: str, request: MaskEditRequest) -> Layer:", new_endpoint + "\ndef edit_mask(project_id: str, request: MaskEditRequest) -> Layer:")

with open("creative-variants-mvp/backend/app/api/projects.py", "w") as f:
    f.write(content)
