bl_info = {
    "name": "Blender Unity Bridge",
    "author": "OpenAI",
    "version": (1, 0, 1),
    "blender": (4, 5, 0),
    "location": "View3D > Sidebar > Unity Export",
    "description": "Export selected static meshes and materials to Unity URP",
    "category": "Import-Export",
}

import bpy
import json
import os
import re
import uuid
from pathlib import Path
from bpy.props import StringProperty, IntProperty, BoolProperty, PointerProperty


def slug(text):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_") or "Asset"


def image_source(socket):
    seen = set()
    while socket and socket.is_linked:
        link = socket.links[0]
        node = link.from_node
        if node.as_pointer() in seen:
            return None
        seen.add(node.as_pointer())
        if node.type == "TEX_IMAGE" and node.image:
            return node.image, "A" if link.from_socket.name == "Alpha" else "RGB", node
        if node.type == "REROUTE":
            socket = node.inputs[0]
            continue
        if node.type in {"SEPARATE_COLOR", "SEPRGB", "SEPXYZ"}:
            channel = {"Red": "R", "Green": "G", "Blue": "B", "R": "R", "G": "G", "B": "B", "X": "R", "Y": "G", "Z": "B"}.get(link.from_socket.name)
            source = image_source(node.inputs[0])
            return (source[0], channel, source[2]) if source and channel else None
        return None
    return None


class Exporter:
    def __init__(self, folder, limit):
        self.folder = folder
        self.limit = limit
        self.warnings = []
        self.samples = {}
        (folder / "Textures").mkdir(exist_ok=True)

    def source(self, socket, label):
        source = image_source(socket)
        if socket.is_linked and not source:
            self.warnings.append(label + ": unsupported nodes; using socket default.")
        if source:
            node = source[2]
            if node.inputs["Vector"].is_linked:
                self.warnings.append(label + ": custom coordinates are not exported; Unity uses UV0.")
            if node.extension != "REPEAT":
                self.warnings.append(label + ": wrapping will be Repeat.")
        return source

    def save_image(self, image, stem, opaque=False):
        if not image.size[0] or not image.size[1]:
            raise RuntimeError("Image has no pixel data: " + image.name)
        relative = "Textures/" + stem + ".png"
        copy = image.copy()
        try:
            if opaque:
                values = list(copy.pixels[:])
                values[3::4] = [1.0] * (len(values) // 4)
                copy.pixels.foreach_set(values)
            w, h = copy.size
            factor = min(1.0, self.limit / max(w, h))
            if factor < 1:
                copy.scale(max(1, round(w * factor)), max(1, round(h * factor)))
            copy.filepath_raw = str(self.folder / relative)
            copy.file_format = "PNG"
            copy.save()
        finally:
            bpy.data.images.remove(copy)
        return relative

    def sample(self, source, x, y, w, h):
        image, channel, _ = source
        key = image.as_pointer()
        if key not in self.samples:
            self.samples[key] = (image.size[0], image.size[1], list(image.pixels[:]))
        iw, ih, pixels = self.samples[key]
        if not iw or not ih:
            raise RuntimeError("Image has no pixel data: " + image.name)
        ix = min(iw - 1, int((x + 0.5) * iw / w))
        iy = min(ih - 1, int((y + 0.5) * ih / h))
        k = (iy * iw + ix) * 4
        if channel in ("R", "G", "B", "A"):
            return pixels[k + "RGBA".index(channel)]
        return 0.2126 * pixels[k] + 0.7152 * pixels[k + 1] + 0.0722 * pixels[k + 2]

    def material(self, mat):
        if not mat.get("unity_bridge_id"):
            mat["unity_bridge_id"] = uuid.uuid4().hex
        mid = str(mat["unity_bridge_id"])
        stem = slug(mat.name) + "_" + mid[:8]
        record = dict(
            id=mid, name=mat.name, baseColor=list(mat.diffuse_color),
            metallic=float(mat.metallic), smoothness=1 - float(mat.roughness),
            emissionColor=[0, 0, 0, 1], emissionStrength=0, normalScale=1,
            transparent=mat.diffuse_color[3] < 0.999,
            doubleSided=not mat.use_backface_culling,
            baseTexture="", normalTexture="", metallicTexture="", emissionTexture="",
        )
        if not mat.use_nodes:
            return record
        outputs = [n for n in mat.node_tree.nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output]
        surface = outputs[0].inputs["Surface"] if outputs else None
        node = surface.links[0].from_node if surface and surface.is_linked else None
        if not node or node.type != "BSDF_PRINCIPLED":
            self.warnings.append(mat.name + ": surface must connect directly to Principled BSDF; exporting viewport colour.")
            return record
        ins = node.inputs
        color, alpha = ins["Base Color"], ins["Alpha"]
        record["baseColor"] = list(color.default_value)
        record["baseColor"][3] = 1.0 if alpha.is_linked else float(alpha.default_value)
        source = self.source(color, mat.name + " / Base Color")
        opacity = self.source(alpha, mat.name + " / Alpha")
        same_alpha = bool(source and opacity and source[0] == opacity[0] and opacity[1] == "A")
        if source:
            if source[1] != "RGB":
                self.warnings.append(mat.name + ": Base Color channel selection is not reproduced; use image Color output.")
            record["baseTexture"] = self.save_image(source[0], stem + "_Base", opaque=not same_alpha)
            record["baseColor"][:3] = [1, 1, 1]
        if same_alpha:
            record["transparent"] = min(list(opacity[0].pixels[:])[3::4], default=1) < 0.999
        else:
            record["transparent"] = not alpha.is_linked and float(alpha.default_value) < 0.999
            if alpha.is_linked:
                self.warnings.append(mat.name + ": unsupported alpha; use the Base Color image Alpha output.")
        met, rough = ins["Metallic"], ins["Roughness"]
        msrc = self.source(met, mat.name + " / Metallic")
        rsrc = self.source(rough, mat.name + " / Roughness")
        record["metallic"] = float(met.default_value)
        record["smoothness"] = 1 - float(rough.default_value)
        if msrc or rsrc:
            sizes = [s[0].size[:] for s in (msrc, rsrc) if s]
            w, h = max(s[0] for s in sizes), max(s[1] for s in sizes)
            if not w or not h:
                raise RuntimeError("Metallic/roughness texture has no pixel data.")
            factor = min(1.0, self.limit / max(w, h))
            w, h = max(1, round(w * factor)), max(1, round(h * factor))
            values = []
            for y in range(h):
                for x in range(w):
                    metallic = self.sample(msrc, x, y, w, h) if msrc else float(met.default_value)
                    roughness = self.sample(rsrc, x, y, w, h) if rsrc else float(rough.default_value)
                    values.extend((max(0, min(1, metallic)), 1, 0, max(0, min(1, 1 - roughness))))
            image = bpy.data.images.new(stem + "_MetalSmooth", width=w, height=h, alpha=True)
            try:
                image.colorspace_settings.name = "Non-Color"
                image.pixels.foreach_set(values)
                record["metallicTexture"] = self.save_image(image, stem + "_MetalSmooth")
            finally:
                bpy.data.images.remove(image)
            record["metallic"] = 1
            record["smoothness"] = 1
        if ins["Normal"].is_linked:
            normal = ins["Normal"].links[0].from_node
            if normal.type == "NORMAL_MAP" and normal.space == "TANGENT":
                nsrc = self.source(normal.inputs["Color"], mat.name + " / Normal")
                if nsrc:
                    record["normalTexture"] = self.save_image(nsrc[0], stem + "_Normal")
                    record["normalScale"] = float(normal.inputs["Strength"].default_value)
                if normal.inputs["Strength"].is_linked:
                    self.warnings.append(mat.name + ": linked normal strength is unsupported.")
            else:
                self.warnings.append(mat.name + ": only tangent-space Normal Map nodes are supported; bump is not baked.")
        emission = ins["Emission Color"]
        esrc = self.source(emission, mat.name + " / Emission")
        record["emissionColor"] = list(emission.default_value)
        record["emissionStrength"] = float(ins["Emission Strength"].default_value)
        if esrc:
            record["emissionTexture"] = self.save_image(esrc[0], stem + "_Emission")
            record["emissionColor"] = [1, 1, 1, 1]
        if ins["Emission Strength"].is_linked:
            self.warnings.append(mat.name + ": linked emission strength is unsupported.")
        for name in ("Transmission Weight", "Coat Weight", "Sheen Weight", "Subsurface Weight"):
            if ins[name].is_linked or float(ins[name].default_value) > 0:
                self.warnings.append(mat.name + ": " + name + " is not translated to URP/Lit.")
        return record


class BridgeSettings(bpy.types.PropertyGroup):
    folder: StringProperty(name="Unity asset folder", subtype="DIR_PATH")
    bundle: StringProperty(name="Bundle name", default="BlenderExport")
    max_size: IntProperty(name="Max texture size", default=2048, min=64, max=8192)
    lightmap_uv: BoolProperty(name="Generate lightmap UVs in Unity", default=False)


class UNITYBRIDGE_OT_export(bpy.types.Operator):
    bl_idname = "unity_bridge.export"
    bl_label = "Export Selected to Unity"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.unity_bridge
        if context.mode != "OBJECT":
            self.report({"ERROR"}, "Switch to Object Mode before exporting.")
            return {"CANCELLED"}
        objects = [o for o in context.selected_objects if o.type == "MESH"]
        if not objects or not settings.folder.strip():
            self.report({"ERROR"}, "Select meshes and choose a Unity asset folder.")
            return {"CANCELLED"}
        root = Path(bpy.path.abspath(settings.folder)).resolve()
        if "Assets" not in root.parts:
            self.report({"ERROR"}, "Destination must be inside your Unity Assets folder.")
            return {"CANCELLED"}
        name = slug(settings.bundle)
        folder = root / name
        original = list(context.selected_objects)
        active = context.view_layer.objects.active
        try:
            folder.mkdir(parents=True, exist_ok=True)
            exporter = Exporter(folder, settings.max_size)
            materials = {}
            for obj in objects:
                if not obj.data.uv_layers:
                    exporter.warnings.append(obj.name + ": no UV map; textures require UV0.")
                for slot in obj.material_slots:
                    if not slot.material:
                        exporter.warnings.append(obj.name + ": empty material slot.")
                    elif slot.material.name not in materials:
                        materials[slot.material.name] = exporter.material(slot.material)
            if len({m["id"] for m in materials.values()}) != len(materials):
                raise RuntimeError("Duplicated material IDs. Remove unity_bridge_id from the duplicated material's Custom Properties and export again.")
            manifest = dict(version=1, model=name + ".fbx", maxTextureSize=settings.max_size,
                            generateLightmapUVs=settings.lightmap_uv,
                            materials=list(materials.values()), warnings=exporter.warnings)
            temporary = folder / (name + ".unitybridge.tmp")
            temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            os.replace(temporary, folder / (name + ".unitybridge.json"))
            bpy.ops.object.select_all(action="DESELECT")
            for obj in objects:
                obj.select_set(True)
            context.view_layer.objects.active = objects[0]
            bpy.ops.export_scene.fbx(
                filepath=str(folder / (name + ".fbx")), use_selection=True, object_types={"MESH"},
                global_scale=1.0, apply_unit_scale=True, apply_scale_options="FBX_SCALE_UNITS",
                axis_forward="-Z", axis_up="Y", use_space_transform=True, bake_space_transform=True,
                use_mesh_modifiers=True, mesh_smooth_type="FACE", use_tspace=True,
                path_mode="STRIP", embed_textures=False, bake_anim=False, add_leaf_bones=False,
            )
            (folder / "ExportWarnings.txt").write_text("\n".join(exporter.warnings) or "No detected warnings.", encoding="utf-8")
            self.report({"INFO"}, f"Exported {len(objects)} meshes; {len(exporter.warnings)} warnings. See ExportWarnings.txt.")
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        finally:
            bpy.ops.object.select_all(action="DESELECT")
            for obj in original:
                obj.select_set(True)
            context.view_layer.objects.active = active
        return {"FINISHED"}


class UNITYBRIDGE_PT_panel(bpy.types.Panel):
    bl_label = "Blender Unity Bridge"
    bl_idname = "UNITYBRIDGE_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Unity Export"

    def draw(self, context):
        settings = context.scene.unity_bridge
        col = self.layout.column()
        col.prop(settings, "folder")
        col.prop(settings, "bundle")
        col.prop(settings, "max_size")
        col.prop(settings, "lightmap_uv")
        col.operator("unity_bridge.export", icon="EXPORT")
        col.label(text="Static meshes / Principled BSDF")


classes = (BridgeSettings, UNITYBRIDGE_OT_export, UNITYBRIDGE_PT_panel)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.unity_bridge = PointerProperty(type=BridgeSettings)


def unregister():
    del bpy.types.Scene.unity_bridge
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
