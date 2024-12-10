import copy
import logging
import random
import time
from pathlib import Path
from typing import Iterable, List

from docling_core.types.doc import CoordOrigin, DocItemLabel
from docling_ibm_models.layoutmodel.layout_predictor import LayoutPredictor
from PIL import Image, ImageDraw

from docling.datamodel.base_models import (
    BoundingBox,
    Cell,
    Cluster,
    LayoutPrediction,
    Page,
)
from docling.datamodel.document import ConversionResult
from docling.datamodel.pipeline_options import AcceleratorOptions
from docling.datamodel.settings import settings
from docling.models.base_model import BasePageModel
from docling.utils.accelerator_utils import decide_device
from docling.utils.layout_postprocessor import LayoutPostprocessor
from docling.utils.profiling import TimeRecorder

_log = logging.getLogger(__name__)


class LayoutModel(BasePageModel):

    TEXT_ELEM_LABELS = [
        DocItemLabel.TEXT,
        DocItemLabel.FOOTNOTE,
        DocItemLabel.CAPTION,
        DocItemLabel.CHECKBOX_UNSELECTED,
        DocItemLabel.CHECKBOX_SELECTED,
        DocItemLabel.SECTION_HEADER,
        DocItemLabel.PAGE_HEADER,
        DocItemLabel.PAGE_FOOTER,
        DocItemLabel.CODE,
        DocItemLabel.LIST_ITEM,
        # "Formula",
    ]
    PAGE_HEADER_LABELS = [DocItemLabel.PAGE_HEADER, DocItemLabel.PAGE_FOOTER]

    TABLE_LABEL = DocItemLabel.TABLE
    FIGURE_LABEL = DocItemLabel.PICTURE
    FORMULA_LABEL = DocItemLabel.FORMULA
    CONTAINER_LABELS = [DocItemLabel.FORM, DocItemLabel.KEY_VALUE_REGION]

    def __init__(self, artifacts_path: Path, accelerator_options: AcceleratorOptions):
        device = decide_device(accelerator_options.device)
        self.layout_predictor = LayoutPredictor(
            artifacts_path, device, accelerator_options.num_threads
        )

    def draw_clusters_and_cells_side_by_side(
        self, conv_res, page, clusters, mode_prefix: str, show: bool = False
    ):
        """
        Draws a page image side by side with clusters filtered into two categories:
        - Left: Clusters excluding FORM, KEY_VALUE_REGION, and PICTURE.
        - Right: Clusters including FORM, KEY_VALUE_REGION, and PICTURE.
        """
        label_to_color = {
            DocItemLabel.TEXT: (255, 255, 153),  # Light Yellow
            DocItemLabel.CAPTION: (255, 204, 153),  # Light Orange
            DocItemLabel.LIST_ITEM: (153, 153, 255),  # Light Purple
            DocItemLabel.FORMULA: (192, 192, 192),  # Gray
            DocItemLabel.TABLE: (255, 204, 204),  # Light Pink
            DocItemLabel.PICTURE: (255, 204, 164),  # Light Beige
            DocItemLabel.SECTION_HEADER: (255, 153, 153),  # Light Red
            DocItemLabel.PAGE_HEADER: (204, 255, 204),  # Light Green
            DocItemLabel.PAGE_FOOTER: (
                204,
                255,
                204,
            ),  # Light Green (same as Page-Header)
            DocItemLabel.TITLE: (255, 153, 153),  # Light Red (same as Section-Header)
            DocItemLabel.FOOTNOTE: (200, 200, 255),  # Light Blue
            DocItemLabel.DOCUMENT_INDEX: (220, 220, 220),  # Light Gray
            DocItemLabel.CODE: (255, 223, 186),  # Peach
            DocItemLabel.CHECKBOX_SELECTED: (255, 182, 193),  # Pale Green
            DocItemLabel.CHECKBOX_UNSELECTED: (255, 182, 193),  # Light Pink
            DocItemLabel.FORM: (200, 255, 255),  # Light Cyan
            DocItemLabel.KEY_VALUE_REGION: (183, 65, 14),  # Rusty orange
        }

        # Filter clusters for left and right images
        exclude_labels = {
            DocItemLabel.FORM,
            DocItemLabel.KEY_VALUE_REGION,
            DocItemLabel.PICTURE,
        }
        left_clusters = [c for c in clusters if c.label not in exclude_labels]
        right_clusters = [c for c in clusters if c.label in exclude_labels]

        # Create a deep copy of the original image for both sides
        left_image = copy.deepcopy(page.image)
        right_image = copy.deepcopy(page.image)

        # Function to draw clusters on an image
        def draw_clusters(image, clusters):
            draw = ImageDraw.Draw(image, "RGBA")
            for c_tl in clusters:
                all_clusters = [c_tl, *c_tl.children]
                for c in all_clusters:
                    cell_color = (0, 0, 0, 40)  # Transparent black for cells
                    for tc in c.cells:
                        cx0, cy0, cx1, cy1 = tc.bbox.as_tuple()
                        draw.rectangle(
                            [(cx0, cy0), (cx1, cy1)],
                            outline=None,
                            fill=cell_color,
                        )

                    x0, y0, x1, y1 = c.bbox.as_tuple()
                    cluster_fill_color = (
                        *list(label_to_color.get(c.label)),  # type: ignore
                        70,
                    )
                    cluster_outline_color = (
                        *list(label_to_color.get(c.label)),  # type: ignore
                        255,
                    )
                    draw.rectangle(
                        [(x0, y0), (x1, y1)],
                        outline=cluster_outline_color,
                        fill=cluster_fill_color,
                    )

        # Draw clusters on both images
        draw_clusters(left_image, left_clusters)
        draw_clusters(right_image, right_clusters)

        # Combine the images side by side
        combined_width = left_image.width * 2
        combined_height = left_image.height
        combined_image = Image.new("RGB", (combined_width, combined_height))
        combined_image.paste(left_image, (0, 0))
        combined_image.paste(right_image, (left_image.width, 0))

        if show:
            combined_image.show()
        else:
            out_path: Path = (
                Path(settings.debug.debug_output_path)
                / f"debug_{conv_res.input.file.stem}"
            )
            out_path.mkdir(parents=True, exist_ok=True)

            out_file = out_path / f"{mode_prefix}_layout_page_{page.page_no:05}.png"
            combined_image.save(str(out_file), format="png")

    def __call__(
        self, conv_res: ConversionResult, page_batch: Iterable[Page]
    ) -> Iterable[Page]:

        for page in page_batch:
            assert page._backend is not None
            if not page._backend.is_valid():
                yield page
            else:
                with TimeRecorder(conv_res, "layout"):
                    assert page.size is not None

                    clusters = []
                    for ix, pred_item in enumerate(
                        self.layout_predictor.predict(page.get_image(scale=1.0))
                    ):
                        label = DocItemLabel(
                            pred_item["label"]
                            .lower()
                            .replace(" ", "_")
                            .replace("-", "_")
                        )  # Temporary, until docling-ibm-model uses docling-core types
                        cluster = Cluster(
                            id=ix,
                            label=label,
                            confidence=pred_item["confidence"],
                            bbox=BoundingBox.model_validate(pred_item),
                            cells=[],
                        )
                        clusters.append(cluster)

                    # DEBUG code:
                    def draw_clusters_and_cells(
                        clusters, mode_prefix: str, show: bool = False
                    ):
                        label_to_color = {
                            DocItemLabel.TEXT: (255, 255, 153),  # Light Yellow
                            DocItemLabel.CAPTION: (255, 204, 153),  # Light Orange
                            DocItemLabel.LIST_ITEM: (153, 153, 255),  # Light Purple
                            DocItemLabel.FORMULA: (192, 192, 192),  # Gray
                            DocItemLabel.TABLE: (255, 204, 204),  # Light Pink
                            DocItemLabel.PICTURE: (255, 255, 204),  # Light Beige
                            DocItemLabel.SECTION_HEADER: (255, 153, 153),  # Light Red
                            DocItemLabel.PAGE_HEADER: (204, 255, 204),  # Light Green
                            DocItemLabel.PAGE_FOOTER: (
                                204,
                                255,
                                204,
                            ),  # Light Green (same as Page-Header)
                            DocItemLabel.TITLE: (
                                255,
                                153,
                                153,
                            ),  # Light Red (same as Section-Header)
                            DocItemLabel.FOOTNOTE: (200, 200, 255),  # Light Blue
                            DocItemLabel.DOCUMENT_INDEX: (220, 220, 220),  # Light Gray
                            DocItemLabel.CODE: (255, 223, 186),  # Peach
                            DocItemLabel.CHECKBOX_SELECTED: (
                                255,
                                182,
                                193,
                            ),  # Pale Green
                            DocItemLabel.CHECKBOX_UNSELECTED: (
                                255,
                                182,
                                193,
                            ),  # Light Pink
                            DocItemLabel.FORM: (200, 255, 255),  # Light Cyan
                            DocItemLabel.KEY_VALUE_REGION: (
                                183,
                                65,
                                14,
                            ),  # Rusty orange
                        }

                        image = copy.deepcopy(page.image)
                        if image is not None:
                            draw = ImageDraw.Draw(image, "RGBA")
                            for c in clusters:
                                cell_color = (0, 0, 0, 40)
                                for tc in c.cells:  # [:1]:
                                    cx0, cy0, cx1, cy1 = tc.bbox.as_tuple()
                                    draw.rectangle(
                                        [(cx0, cy0), (cx1, cy1)],
                                        outline=None,
                                        fill=cell_color,
                                    )

                                x0, y0, x1, y1 = c.bbox.as_tuple()
                                cluster_fill_color = (
                                    *list(label_to_color.get(c.label)),  # type: ignore
                                    70,
                                )
                                cluster_outline_color = (
                                    *list(label_to_color.get(c.label)),  # type: ignore
                                    255,
                                )
                                draw.rectangle(
                                    [(x0, y0), (x1, y1)],
                                    outline=cluster_outline_color,
                                    fill=cluster_fill_color,
                                )

                            if show:
                                image.show()
                            else:
                                out_path: Path = (
                                    Path(settings.debug.debug_output_path)
                                    / f"debug_{conv_res.input.file.stem}"
                                )
                                out_path.mkdir(parents=True, exist_ok=True)

                                out_file = (
                                    out_path
                                    / f"{mode_prefix}_layout_page_{page.page_no:05}.png"
                                )
                                image.save(str(out_file), format="png")

                    if settings.debug.visualize_raw_layout:
                        self.draw_clusters_and_cells_side_by_side(
                            conv_res, page, clusters, mode_prefix="raw"
                        )

                    # Apply postprocessing
                    processed_clusters, processed_cells = LayoutPostprocessor(
                        page.cells, clusters
                    ).postprocess()
                    # processed_clusters, processed_cells = clusters, page.cells

                    page.cells = processed_cells
                    page.predictions.layout = LayoutPrediction(
                        clusters=processed_clusters
                    )

                if settings.debug.visualize_layout:
                    self.draw_clusters_and_cells_side_by_side(
                        conv_res, page, processed_clusters, mode_prefix="postprocessed"
                    )

                yield page
