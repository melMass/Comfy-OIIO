from logging import getLogger
from pathlib import Path

import folder_paths
import numpy as np
import OpenImageIO as oiio
import torch
from OpenImageIO import ImageSpec
from OpenImageIO.OpenImageIO import ROI

log = getLogger("comfy-oiio")

DEFAULT_INPUT_TRANSFORM = "scene_linear"
DEFAULT_OUTPUT_TRANSFORM = "sRGB"

# TODO: Add support for sublayers / arbitrary channels
# TODO: Support Display/View transforms? (probably not easy to adapt to comfy)
# TODO: Better error handling


class OIIO_LoadImage:
    @classmethod
    def INPUT_TYPES(cls):
        colorspaces = OIIO_ColorspaceConvert.get_colorspaces()
        return {
            "required": {
                "filepath": ("STRING", {"default": ""}),
                "precision": (["auto", "uint8", "half", "float"], {"default": "auto"}),
                "input_transform": (
                    colorspaces,
                    {"default": "auto"},
                ),
            }
        }

    RETURN_TYPES = ("IMAGE", "MASK", "INT", "INT")
    RETURN_NAMES = ("image", "mask", "xres", "yres")
    FUNCTION = "read"
    CATEGORY = "oiio"

    def read(self, filepath, precision, input_transform):
        path = Path(filepath)

        if path.is_dir():
            pixels_list = []
            masks_list = []
            xres = yres = None
            for f in sorted(path.iterdir()):
                try:
                    result = self.read_single(f, precision, input_transform)
                    if result is None:
                        continue

                    # use the first match as reference for dimensions
                    if xres is None:
                        xres = result[2]
                        yres = result[3]
                    elif (result[2], result[3]) != (xres, yres):
                        print(f"Warning: Skipping {f}, dimensions don't match")
                        continue

                    pixels_list.append(result[0])
                    masks_list.append(result[1])

                except Exception as e:
                    print(f"Warning: Couldn't read {f}: {e}")
                    continue

            if not pixels_list:
                raise ValueError(f"No readable images found in {filepath}")

            # stack
            pixels_tensor = torch.cat(pixels_list, dim=0)
            mask_tensor = torch.cat(masks_list, dim=0)

            return (pixels_tensor, mask_tensor, xres, yres)

        else:
            return self.read_single(path, precision, input_transform)

    def read_single(self, filepath, precision, input_transform):
        """Read a single image file."""
        img = oiio.ImageInput.open(str(filepath))
        if img:
            spec = img.spec()
            if input_transform != "auto":
                spec.attribute("oiio:ColorSpace", input_transform)

            precision = spec.format if precision == "auto" else precision
            xres = spec.width
            yres = spec.height
            nchannels = spec.nchannels

            pixels = img.read_image(0, 0, 0, nchannels, precision)
            img.close()

            rgb = pixels[:, :, :3] if nchannels > 3 else pixels
            pixels_tensor = torch.from_numpy(rgb)

            if pixels_tensor.dtype == torch.uint8:
                pixels_tensor = pixels_tensor.float() / 255.0

            if pixels_tensor.dim() == 3:
                pixels_tensor = pixels_tensor.unsqueeze(0)

            if nchannels > 3:
                mask_tensor = torch.from_numpy(pixels[:, :, 3]).float()
                if mask_tensor.dtype == torch.uint8:
                    mask_tensor = mask_tensor / 255.0
            else:
                mask_tensor = torch.ones((yres, xres), dtype=torch.float32)

            mask_tensor = mask_tensor.unsqueeze(0)

            return (pixels_tensor, mask_tensor, xres, yres)

        return None


class OIIO_ColorspaceConvert:
    @classmethod
    def get_colorspaces(cls):
        common = [
            "scene_linear",
            "lin_srgb",
            "lin_rec709",
            "Rec709",
            "sRGB",
            "linear",
            "ACEScg",
            "AdobeRGB",
            "KodakLog",
            "g22_rec709",
            "g18_rec709",
            "Gamma2.4",
        ]
        colorspaces = []
        try:
            config = oiio.ColorConfig()
            colorspaces = config.getColorSpaceNames()
        except Exception as e:
            print(f"Warning: Could not get colorspaces: {e}")

        return ["auto"] + common + colorspaces

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "input_colorspace": (cls.get_colorspaces(), {"default": "sRGB"}),
                "output_colorspace": (cls.get_colorspaces(), {"default": DEFAULT_OUTPUT_TRANSFORM}),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "convert"
    CATEGORY = "oiio"

    def convert(self, image, input_colorspace, output_colorspace):
        pixels = image.squeeze(0).cpu().numpy()
        spec = oiio.ImageSpec(pixels.shape[1], pixels.shape[0], pixels.shape[2], oiio.FLOAT)
        if input_colorspace != "auto":
            spec.attribute("oiio:ColorSpace", input_colorspace)

        in_buf = oiio.ImageBuf(spec)
        in_buf.set_pixels(ROI(), pixels)

        out_spec = oiio.ImageSpec(spec)
        out_spec.attribute("oiio:ColorSpace", output_colorspace)
        out_buf = oiio.ImageBuf(out_spec)

        # buf.set_write_format(oiio.FLOAT)
        # buf.copy_pixels(pixels)
        # buf.specmod().attribute("oiio:ColorSpace", input_colorspace)

        oiio.ImageBufAlgo.colorconvert(out_buf, in_buf, input_colorspace, output_colorspace)

        result = torch.from_numpy(out_buf.get_pixels()).unsqueeze(0)

        return (result,)


class OIIO_SaveImage:
    @classmethod
    def INPUT_TYPES(cls):
        colorspaces = OIIO_ColorspaceConvert.get_colorspaces()
        return {
            "required": {
                "images": ("IMAGE",),
                "filepath": ("STRING", {"default": "output.exr"}),
                "precision": (["half", "float"], {"default": "half"}),
                "compression": (
                    ["none", "zip", "zips", "piz", "pxr24", "b44", "b44a", "dwaa", "dwab"],
                    {"default": "zip"},
                ),
                "colorspace": (
                    colorspaces,
                    {"default": "auto"},
                ),
                "frame_start": ("INT", {"default": 1, "min": 0, "max": 999999}),
                "frame_pad": ("INT", {"default": 4, "min": 1, "max": 8}),
                "dwa_compression_level": ("FLOAT", {"default": 45.0, "min": 10.0, "max": 250.0}),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    RETURN_TYPES = ()
    FUNCTION = "write"
    CATEGORY = "oiio"
    EXPERIMENTAL = True
    OUTPUT_NODE = True

    def write(
        self,
        images,
        filepath,
        precision,
        compression,
        colorspace,
        frame_start,
        frame_pad,
        dwa_compression_level,
        prompt=None,
        extra_pnginfo=None,
    ):
        _path = Path(filepath)
        counter = 0
        if _path.is_absolute():
            output_dir = _path.parent
            filename = _path.stem
            output_dir.mkdir(parents=True, exist_ok=True)
        else:
            c_out = folder_paths.get_output_directory()
            output_dir, filename, counter, subfolder, filename_prefix = (
                folder_paths.get_save_image_path(filepath, c_out, images.shape[2], images.shape[1])
            )
            output_dir = Path(output_dir)

        for i in range(images.shape[0]):
            if _path.is_absolute():
                frame_num = frame_start + i
                output_path = output_dir / f"{filename}.{frame_num:0{frame_pad}d}.exr"
            else:
                output_path = output_dir / f"{filename}_{counter + i:0{frame_pad}d}.exr"

            pixels = images[i].cpu().numpy()

            spec = ImageSpec(pixels.shape[1], pixels.shape[0], pixels.shape[2], precision)
            spec.attribute("compression", compression)
            if compression in ["dwaa", "dwab"]:
                spec.attribute("compressionlevel", dwa_compression_level)

            if colorspace != "auto":
                spec.set_colorspace(colorspace)

            if pixels.shape[2] == 1:
                spec.channelnames = ("A",)
            elif pixels.shape[2] == 3:
                spec.channelnames = ("R", "G", "B")
            elif pixels.shape[2] == 4:
                spec.channelnames = ("R", "G", "B", "A")

            out = ImageOutput.create(str(output_path))
            if out:
                try:
                    out.open(str(output_path), spec)
                    out.write_image(pixels)
                finally:
                    out.close()

        return ()


NODE_CLASS_MAPPINGS = {
    "OIIO_LoadImage": OIIO_LoadImage,
    "OIIO_ColorspaceConvert": OIIO_ColorspaceConvert,
    "OIIO_SaveImage": OIIO_SaveImage,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "OIIO_ColorspaceConvert": "Colorspace Convert (OIIO)",
    "OIIO_LoadImage": "Load Image (OIIO)",
    "OIIO_SaveImage": "Save Image (OIIO)",
}
