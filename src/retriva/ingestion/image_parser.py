# Copyright (C) 2026 Andrea Marson (am.dev.75@gmail.com)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from bs4 import BeautifulSoup
from pathlib import Path
from typing import Callable, List, Optional
from retriva.domain.models import ImageContext
from retriva.registry import CapabilityRegistry
from retriva.logger import get_logger

# Import module to trigger default registration
import retriva.ingestion.vlm_describer  # noqa: F401 — registers DefaultVLMDescriber

logger = get_logger(__name__)


def extract_images_from_html(html: str) -> List[ImageContext]:
    """
    Extracts <img> elements from HTML and their surrounding context.
    Excludes images located inside structural boilerplate elements.
    """
    soup = BeautifulSoup(html, "html.parser")
    images = []
    
    for element in soup(["nav", "footer", "script", "style", "aside", "header"]):
        element.decompose()
        
    for img in soup.find_all("img"):
        src = img.get("src")
        if not src:
            continue
        alt = img.get("alt", "").strip()
        
        caption = ""
        parent_figure = img.find_parent("figure")
        if parent_figure:
            figcaption = parent_figure.find("figcaption")
            if figcaption:
                caption = figcaption.get_text(strip=True)
                
        parent = img.parent
        surrounding_text = parent.get_text(separator=" ", strip=True)[:200] if parent else ""
        
        if "icon" in src.lower() or "logo" in src.lower():
            continue
            
        images.append(ImageContext(
            src=src,
            alt=alt,
            caption=caption,
            surrounding_text=surrounding_text
        ))
        
    logger.debug(f"Discovered {len(images)} valid images from HTML content.")
    return images


def resolve_image_path(src: str, html_file_path: str) -> Optional[Path]:
    """
    Resolves a relative <img src> attribute against the HTML file's
    directory on disk.  Returns None if the resolved file does not exist.
    """
    if not html_file_path:
        return None

    html_dir = Path(html_file_path).parent
    candidate = (html_dir / src).resolve()

    if candidate.is_file():
        return candidate

    logger.debug(f"Could not resolve image src '{src}' relative to '{html_dir}'")
    return None


def enrich_images_with_vlm(
    images: List[ImageContext],
    html_file_path: str,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> None:
    """
    For each ImageContext, resolves its src to a local file and calls the
    VLM to generate a detailed text description.  Mutates the list in place.
    """
    if not html_file_path:
        logger.debug("No origin file path — skipping VLM enrichment.")
        return

    vlm = CapabilityRegistry().get_instance("vlm_describer")

    for img in images:
        if cancel_check and cancel_check():
            from retriva.ingestion_api.execution import CancellationError
            raise CancellationError("Job cancelled during VLM enrichment")

        resolved = resolve_image_path(img.src, html_file_path)
        if resolved is None:
            continue
        description = vlm.describe(resolved)
        if description:
            img.vlm_description = description
            logger.info(f"VLM enriched '{img.src}' ({len(description)} chars)")
