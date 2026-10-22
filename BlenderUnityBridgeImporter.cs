// Save as Assets/Editor/BlenderUnityBridgeImporter.cs.
// Intended for Unity 6 with URP configured. Editor-only; no runtime component.
using System;
using System.IO;
using System.Linq;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Rendering;
using UnityEditor;

namespace BlenderUnityBridge
{
    [Serializable]
    public class Manifest
    {
        public int version;
        public string model;
        public int maxTextureSize = 2048;
        public bool generateLightmapUVs;
        public MaterialRecord[] materials;
        public string[] warnings;
    }

    [Serializable]
    public class MaterialRecord
    {
        public string id, name, baseTexture, normalTexture, metallicTexture, emissionTexture;
        public float[] baseColor, emissionColor;
        public float metallic, smoothness, emissionStrength, normalScale = 1;
        public bool transparent, doubleSided;
    }

    public class BridgeImporter : AssetPostprocessor
    {
        static readonly HashSet<string> Pending = new HashSet<string>();
        static bool scheduled;

        static string Normalize(string path) => path.Replace('\\', '/');
        static string Sidecar(string fbx) => Normalize(Path.ChangeExtension(fbx, ".unitybridge.json"));

        static Manifest Read(string path)
        {
            var manifest = JsonUtility.FromJson<Manifest>(File.ReadAllText(path));
            if (manifest == null || manifest.version != 1 || manifest.materials == null)
                throw new InvalidDataException("Unsupported or incomplete bridge manifest: " + path);
            return manifest;
        }

        void OnPreprocessModel()
        {
            if (!assetPath.EndsWith(".fbx", StringComparison.OrdinalIgnoreCase) || !File.Exists(Sidecar(assetPath)))
                return;
            try
            {
                var manifest = Read(Sidecar(assetPath));
                var importer = (ModelImporter)assetImporter;
                importer.globalScale = 1;
                importer.useFileScale = true;
                importer.importAnimation = false;
                importer.animationType = ModelImporterAnimationType.None;
                importer.isReadable = false;
                importer.importCameras = false;
                importer.importLights = false;
                importer.importBlendShapes = false;
                importer.importNormals = ModelImporterNormals.Import;
                importer.importTangents = ModelImporterTangents.CalculateMikk;
                importer.generateSecondaryUV = manifest.generateLightmapUVs;
                importer.materialImportMode = ModelImporterMaterialImportMode.ImportStandard;
            }
            catch (Exception ex)
            {
                Debug.LogError("Blender Unity Bridge: " + ex.Message);
            }
        }

        static void OnPostprocessAllAssets(string[] imported, string[] deleted, string[] moved, string[] movedFrom)
        {
            foreach (var path in imported.Concat(moved))
            {
                if (path.EndsWith(".unitybridge.json", StringComparison.OrdinalIgnoreCase))
                    Pending.Add(path);
                else if (path.EndsWith(".fbx", StringComparison.OrdinalIgnoreCase) && File.Exists(Sidecar(path)))
                    Pending.Add(Sidecar(path));
            }
            if (Pending.Count == 0 || scheduled)
                return;
            scheduled = true;
            EditorApplication.delayCall += Drain;
        }

        static void Drain()
        {
            scheduled = false;
            var work = Pending.ToArray();
            Pending.Clear();
            foreach (var path in work)
            {
                try
                {
                    if (File.Exists(path))
                        Build(path);
                }
                catch (Exception ex)
                {
                    Debug.LogError("Blender Unity Bridge: " + path + "\n" + ex);
                }
            }
        }

        [MenuItem("Tools/Blender Unity Bridge/Rebuild All Exported Materials")]
        public static void RebuildAll()
        {
            foreach (var path in Directory.GetFiles("Assets", "*.unitybridge.json", SearchOption.AllDirectories))
            {
                try { Build(Normalize(path)); }
                catch (Exception ex) { Debug.LogError("Blender Unity Bridge: " + ex); }
            }
        }

        static Color ColorOf(float[] values, Color fallback)
        {
            return values != null && values.Length >= 3
                ? new Color(values[0], values[1], values[2], values.Length >= 4 ? values[3] : 1)
                : fallback;
        }

        static string RelativeAsset(string folder, string relative)
        {
            if (string.IsNullOrEmpty(relative))
                throw new InvalidDataException("Empty model or texture path.");
            string full = Path.GetFullPath(Path.Combine(folder, relative));
            string root = Path.GetFullPath(folder) + Path.DirectorySeparatorChar;
            if (!full.StartsWith(root, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("Model/texture path must remain inside the export bundle.");
            string project = Path.GetFullPath(".") + Path.DirectorySeparatorChar;
            if (!full.StartsWith(project, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("Path is outside the Unity project.");
            return Normalize(full.Substring(project.Length));
        }

        static Texture2D LoadTexture(string folder, string relative, bool srgb, bool normal, int maxSize)
        {
            if (string.IsNullOrEmpty(relative))
                return null;
            string path = RelativeAsset(folder, relative);
            if (!File.Exists(path))
                throw new FileNotFoundException("Missing exported texture", path);
            if (AssetImporter.GetAtPath(path) == null)
                AssetDatabase.ImportAsset(path, ImportAssetOptions.ForceSynchronousImport);
            var importer = AssetImporter.GetAtPath(path) as TextureImporter;
            if (importer == null)
                throw new InvalidDataException("Unsupported texture: " + path);

            string before = EditorJsonUtility.ToJson(importer);
            importer.textureType = normal ? TextureImporterType.NormalMap : TextureImporterType.Default;
            importer.sRGBTexture = srgb;
            importer.alphaSource = TextureImporterAlphaSource.FromInput;
            importer.alphaIsTransparency = srgb && !normal;
            importer.mipmapEnabled = true;
            importer.isReadable = false;
            importer.wrapMode = TextureWrapMode.Repeat;
            importer.filterMode = FilterMode.Bilinear;
            importer.maxTextureSize = Mathf.ClosestPowerOfTwo(Mathf.Clamp(maxSize, 64, 8192));
            importer.textureCompression = TextureImporterCompression.Compressed;
            importer.convertToNormalmap = false;
            importer.flipGreenChannel = false;
            // Android overrides remain controlled by the project's platform settings.
            if (before != EditorJsonUtility.ToJson(importer))
                importer.SaveAndReimport();
            return AssetDatabase.LoadAssetAtPath<Texture2D>(path);
        }

        static void Keyword(Material material, string name, bool enabled)
        {
            if (enabled) material.EnableKeyword(name);
            else material.DisableKeyword(name);
        }

        static void Build(string sidecar)
        {
            var data = Read(sidecar);
            string folder = Normalize(Path.GetDirectoryName(sidecar));
            string modelPath = RelativeAsset(folder, data.model);
            if (!File.Exists(modelPath))
                return; // Blender may still be writing the first export.

            var shader = Shader.Find("Universal Render Pipeline/Lit");
            if (!shader)
                throw new InvalidOperationException("URP/Lit was not found. Configure URP first.");
            if (AssetImporter.GetAtPath(modelPath) == null)
                AssetDatabase.ImportAsset(modelPath, ImportAssetOptions.ForceSynchronousImport);
            var importer = AssetImporter.GetAtPath(modelPath) as ModelImporter;
            if (importer == null)
                throw new InvalidDataException("Manifest model is not an FBX model.");

            string materialFolder = folder + "/Materials";
            if (!AssetDatabase.IsValidFolder(materialFolder))
                AssetDatabase.CreateFolder(folder, "Materials");
            var ids = new HashSet<string>();
            var remaps = importer.GetExternalObjectMap();
            bool remapChanged = false;

            foreach (var record in data.materials)
            {
                Guid parsed;
                if (!Guid.TryParse(record.id, out parsed) || !ids.Add(record.id))
                    throw new InvalidDataException("Invalid or duplicate material ID: " + record.name);
                string path = materialFolder + "/" + record.id + ".mat";
                var material = AssetDatabase.LoadAssetAtPath<Material>(path);
                if (!material)
                {
                    material = new Material(shader);
                    AssetDatabase.CreateAsset(material, path);
                }
                material.name = record.name;
                material.shader = shader;

                var baseTex = LoadTexture(folder, record.baseTexture, true, false, data.maxTextureSize);
                var normalTex = LoadTexture(folder, record.normalTexture, false, true, data.maxTextureSize);
                var metalTex = LoadTexture(folder, record.metallicTexture, false, false, data.maxTextureSize);
                var emissionTex = LoadTexture(folder, record.emissionTexture, true, false, data.maxTextureSize);

                material.SetFloat("_WorkflowMode", 1);
                material.SetColor("_BaseColor", ColorOf(record.baseColor, Color.white));
                material.SetTexture("_BaseMap", baseTex);
                material.SetTextureScale("_BaseMap", Vector2.one);
                material.SetTextureOffset("_BaseMap", Vector2.zero);
                material.SetTexture("_BumpMap", normalTex);
                material.SetFloat("_BumpScale", record.normalScale);
                material.SetTexture("_MetallicGlossMap", metalTex);
                material.SetFloat("_Metallic", record.metallic);
                material.SetFloat("_Smoothness", record.smoothness);
                material.SetFloat("_SmoothnessTextureChannel", 0);
                material.SetTexture("_EmissionMap", emissionTex);
                var emission = ColorOf(record.emissionColor, Color.black) * record.emissionStrength;
                emission.a = 1;
                material.SetColor("_EmissionColor", emission);
                material.globalIlluminationFlags = emission.maxColorComponent > 0
                    ? MaterialGlobalIlluminationFlags.BakedEmissive
                    : MaterialGlobalIlluminationFlags.EmissiveIsBlack;

                Keyword(material, "_NORMALMAP", normalTex != null);
                Keyword(material, "_METALLICSPECGLOSSMAP", metalTex != null);
                Keyword(material, "_SPECULAR_SETUP", false);
                Keyword(material, "_SMOOTHNESS_TEXTURE_ALBEDO_CHANNEL_A", false);
                Keyword(material, "_EMISSION", emission.maxColorComponent > 0);

                bool transparent = record.transparent;
                material.SetFloat("_Surface", transparent ? 1 : 0);
                material.SetFloat("_Blend", 0);
                material.SetFloat("_AlphaClip", 0);
                material.SetFloat("_SrcBlend", (float)(transparent ? BlendMode.SrcAlpha : BlendMode.One));
                material.SetFloat("_DstBlend", (float)(transparent ? BlendMode.OneMinusSrcAlpha : BlendMode.Zero));
                material.SetFloat("_SrcBlendAlpha", (float)BlendMode.One);
                material.SetFloat("_DstBlendAlpha", (float)(transparent ? BlendMode.OneMinusSrcAlpha : BlendMode.Zero));
                material.SetFloat("_ZWrite", transparent ? 0 : 1);
                material.SetFloat("_Cull", (float)(record.doubleSided ? CullMode.Off : CullMode.Back));
                material.SetFloat("_QueueOffset", 0);
                material.SetOverrideTag("RenderType", transparent ? "Transparent" : "Opaque");
                material.renderQueue = transparent ? (int)RenderQueue.Transparent : -1;
                Keyword(material, "_SURFACE_TYPE_TRANSPARENT", transparent);
                Keyword(material, "_ALPHATEST_ON", false);
                Keyword(material, "_ALPHAPREMULTIPLY_ON", false);
                Keyword(material, "_ALPHAMODULATE_ON", false);
                material.SetShaderPassEnabled("ShadowCaster", !transparent);
                material.SetShaderPassEnabled("DepthOnly", !transparent);
                EditorUtility.SetDirty(material);

                var identifier = new AssetImporter.SourceAssetIdentifier(typeof(Material), record.name);
                UnityEngine.Object mapped;
                if (!remaps.TryGetValue(identifier, out mapped) || mapped != material)
                {
                    importer.AddRemap(identifier, material);
                    remapChanged = true;
                }
            }

            AssetDatabase.SaveAssets();
            if (remapChanged)
                importer.SaveAndReimport();

            var model = AssetDatabase.LoadAssetAtPath<GameObject>(modelPath);
            if (!model)
                throw new InvalidDataException("Imported model could not be loaded: " + modelPath);
            var expected = new HashSet<string>(data.materials.Select(record => record.name));
            foreach (var material in model.GetComponentsInChildren<Renderer>(true)
                         .SelectMany(renderer => renderer.sharedMaterials)
                         .Where(material => material != null).Distinct())
            {
                if (expected.Contains(material.name) &&
                    !AssetDatabase.GetAssetPath(material).StartsWith(materialFolder + "/", StringComparison.Ordinal))
                    Debug.LogWarning("Blender Unity Bridge: material '" + material.name +
                                     "' was not remapped. Check its FBX material name in the model's Materials tab.", material);
            }
        }
    }
}
