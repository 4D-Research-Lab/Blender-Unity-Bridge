# Blender → Unity URP Bridge

First implementation for **Blender 4.5 / Unity 6 URP**, targeting static models for Quest VR.

## Package contents

- `blender_unity_bridge.py` — Blender add-on.
- `BlenderUnityBridgeImporter.cs` — Unity Editor automation.
- `README.md` — full installation guide, supported material layouts, limitations, and troubleshooting.

## Install

1. Put `BlenderUnityBridgeImporter.cs` in your Unity project's `Assets/Editor` folder. Let Unity compile it. URP must already be configured.
2. In Blender Preferences → Add-ons, use **Install from Disk** in the dropdown menu to install `blender_unity_bridge.py`, then enable it.
3. In Blender's viewport, press **N** and open **Unity Export**.
4. Choose a folder inside your Unity project's **Assets** directory.
5. Enter a bundle name, select meshes, and click **Export Selected to Unity**.
6. Return to Unity. After import finishes, drag the FBX into your scene.

## Re-export

Use the same destination and bundle name. Existing generated Unity materials are updated in place, preserving their asset GUIDs. Save your Blender file after the initial export so the material IDs are retained.

Changes to generated material settings in Unity are overwritten on export. For a Unity-only variant, duplicate a generated material and assign that copy. Manually overridden scene/prefab material slots are not forcibly replaced.

## Material support

| Blender | Unity URP/Lit |
|---|---|
| Principled Base Color / colour image | Base Color / Base Map |
| Metallic scalar or texture | Metallic |
| Roughness scalar or texture | Converted to smoothness |
| Tangent-space Normal Map node | Normal Map and strength |
| Emission colour/image and scalar strength | Emission |
| Constant alpha or Base Color image alpha | Basic alpha-blended transparency |

Metallic and smoothness textures are packed into Unity's red/alpha layout. Textures are exported as PNG, with a default maximum dimension of 2048.

## Boundaries

- Static mesh selection only; no rigging or animation.
- No procedural material baking, custom shader translation, separate opacity textures, or alpha cutout.
- Uses UV0; custom mapping transforms are not reproduced.
- Blender colours/emission should use sRGB textures; metallic/roughness/normals should use Non-Color.
- Unsupported material features produce warnings where detected. Read `ExportWarnings.txt`.
- No automatic decimation, LODs, colliders, atlasing, or Quest performance guarantees.
- Android texture compression overrides remain controlled by your Unity project.
- Materials are reused within each export bundle, not globally across different bundles.

## First test

Export a **one-metre cube** and a small textured mesh first. Check scale, orientation, material assignment, normals, and transparency in Unity and on Quest before exporting an entire scene.

Use **Tools → Blender Unity Bridge → Rebuild All Exported Materials** if materials need rebuilding. Preserve Unity `.meta` files to retain references.

**Validation:** Python syntax was checked. Blender export, Unity compilation/import, and Quest rendering have not been run in this environment; this is a testable first implementation, not a runtime-verified release.
