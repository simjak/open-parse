from typing import (
    Literal,
    Optional,
    Sequence,
    Any,
    DefaultDict,
    TypedDict,
    List,
    Tuple,
    Union,
)
from collections import defaultdict, namedtuple
from functools import cache
from enum import Enum
import re


from pydantic import BaseModel, model_validator, computed_field, ConfigDict

from src.utils import num_tokens
from src import consts

AggregatePosition = namedtuple("AggregatePosition", ["min_page", "min_y0", "min_x0"])


class PrevNodeSimilarity(TypedDict):
    prev_similarity: float
    node: "Node"


class NodeVariant(Enum):
    TEXT = "text"
    TABLE = "table"
    IMAGE = "image"


class Bbox(BaseModel):
    page: int
    page_height: float
    page_width: float
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def area(self) -> float:
        return (self.x1 - self.x0) * (self.y1 - self.y0)

    @model_validator(mode="before")
    @classmethod
    def x1_must_be_greater_than_x0(cls, data: Any) -> Any:
        if "x0" in data and data["x1"] <= data["x0"]:
            raise ValueError("x1 must be greater than x0")
        return data

    @model_validator(mode="before")
    @classmethod
    def y1_must_be_greater_than_y0(cls, data: Any) -> Any:
        if "y0" in data and data["y1"] <= data["y0"]:
            raise ValueError("y1 must be greater than y0")
        return data

    def combine(self, other: "Bbox") -> "Bbox":
        if self.page != other.page:
            raise ValueError("Bboxes must be from the same page to combine.")
        return Bbox(
            page=self.page,
            page_height=self.page_height,
            page_width=self.page_width,
            x0=min(self.x0, other.x0),
            y0=min(self.y0, other.y0),
            x1=max(self.x1, other.x1),
            y1=max(self.y1, other.y1),
        )

    model_config = ConfigDict(frozen=True)


#####################
### TEXT ELEMENTS ###
#####################


class TextSpan(BaseModel):
    text: str
    is_bold: bool
    is_italic: bool
    size: float

    @property
    @cache
    def is_heading(self) -> bool:
        MIN_HEADING_SIZE = 16
        return self.size >= MIN_HEADING_SIZE and self.is_bold

    def formatted_text(
        self,
        previous_span: Optional["TextSpan"] = None,
        next_span: Optional["TextSpan"] = None,
    ) -> str:
        """Format text considering adjacent spans to avoid redundant markdown symbols."""
        formatted = self.text

        # Check if style changes at the beginning
        if self.is_bold and (previous_span is None or not previous_span.is_bold):
            formatted = f"**{formatted}"
        if self.is_italic and (previous_span is None or not previous_span.is_italic):
            formatted = f"*{formatted}"

        # Check if style changes at the end
        if self.is_bold and (next_span is None or not next_span.is_bold):
            formatted = f"{formatted}**"
        if self.is_italic and (next_span is None or not next_span.is_italic):
            formatted = f"{formatted}*"

        return formatted

    model_config = ConfigDict(frozen=True)


class LineElement(BaseModel):
    bbox: Tuple[float, float, float, float]
    spans: Tuple[TextSpan, ...]
    style: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def round_bbox_vals(cls, data: Any) -> Any:
        data["bbox"] = tuple(round(val, 2) for val in data["bbox"])
        return data

    @computed_field  # type: ignore
    @property
    def text(self) -> str:
        """
        Combine spans into a single text string, respecting markdown syntax.
        """
        if not self.spans:
            return ""

        combined_text = ""
        for i, span in enumerate(self.spans):
            previous_span = self.spans[i - 1] if i > 0 else None
            next_span = self.spans[i + 1] if i < len(self.spans) - 1 else None
            combined_text += span.formatted_text(previous_span, next_span)

        cleaned_text = self._clean_markdown_formatting(combined_text)
        return cleaned_text

    @property
    @cache
    def is_bold(self) -> bool:
        # ignore last span for formatting, often see weird trailing spans
        spans = self.spans[:-1] if len(self.spans) > 1 else self.spans

        return all(span.is_bold for span in spans)

    @property
    @cache
    def is_italic(self) -> bool:
        # ignore last span for formatting, often see weird trailing spans
        spans = self.spans[:-1] if len(self.spans) > 1 else self.spans
        return all(span.is_italic for span in spans)

    @property
    @cache
    def is_heading(self) -> bool:
        # ignore last span for formatting, often see weird trailing spans
        spans = self.spans[:-1] if len(self.spans) > 1 else self.spans
        MIN_HEADING_SIZE = 16
        return all(span.size >= MIN_HEADING_SIZE and span.is_bold for span in spans)

    def _clean_markdown_formatting(self, text: str) -> str:
        """
        Uses regex to clean up markdown formatting, ensuring symbols don't surround whitespace.
        This will fix issues with bold (** or __) and italic (* or _) markdown where there may be
        spaces between the markers and the text.
        """
        patterns = [
            (
                r"(\*\*|__)\s+",
                r"\1",
            ),  # Remove space after opening bold or italic marker
            (
                r"\s+(\*\*|__)",
                r"\1",
            ),  # Remove space before closing bold or italic marker
            (r"(\*|_)\s+", r"\1"),  # Remove space after opening italic marker
            (r"\s+(\*|_)", r"\1"),  # Remove space before closing italic marker
            (
                r"(\*\*|__)(\*\*|__)",
                r"\1 \2",
            ),  # Add a space between adjacent identical markers
        ]

        cleaned_text = text
        for pattern, replacement in patterns:
            cleaned_text = re.sub(pattern, replacement, cleaned_text)

        return cleaned_text

    def overlaps(self, other: "LineElement", error_margin: float = 0.0) -> bool:
        x_overlap = not (
            self.bbox[0] - error_margin > other.bbox[2] + error_margin
            or other.bbox[0] - error_margin > self.bbox[2] + error_margin
        )

        y_overlap = not (
            self.bbox[1] - error_margin > other.bbox[3] + error_margin
            or other.bbox[1] - error_margin > self.bbox[3] + error_margin
        )

        return x_overlap and y_overlap

    def is_at_similar_height(
        self, other: "LineElement", error_margin: float = 0.0
    ) -> bool:
        y_distance = abs(self.bbox[1] - other.bbox[1])

        return y_distance <= error_margin

    def combine(self, other: "LineElement") -> "LineElement":
        """
        Used for spans
        """
        new_bbox = (
            min(self.bbox[0], other.bbox[0]),
            min(self.bbox[1], other.bbox[1]),
            max(self.bbox[2], other.bbox[2]),
            max(self.bbox[3], other.bbox[3]),
        )
        new_spans = tuple(self.spans + other.spans)

        return LineElement(bbox=new_bbox, spans=new_spans)

    model_config = ConfigDict(frozen=True)


class TextElement(BaseModel):
    text: str
    lines: Tuple[LineElement, ...]
    bbox: Bbox
    variant: Literal[NodeVariant.TEXT] = NodeVariant.TEXT

    def is_at_similar_height(
        self, other: Union["TableElement", "TextElement"], error_margin: float = 1
    ) -> bool:
        y_distance = abs(self.bbox.y1 - other.bbox.y1)

        return y_distance <= error_margin

    @property
    def tokens(self) -> int:
        return num_tokens(self.text)

    @property
    def page(self) -> int:
        return self.bbox.page

    @property
    def area(self) -> float:
        return (self.bbox.x1 - self.bbox.x0) * (self.bbox.y1 - self.bbox.y0)

    def overlaps(
        self,
        other: "TextElement",
        x_error_margin: float = 0.0,
        y_error_margin: float = 0.0,
    ) -> bool:
        if self.page != other.page:
            return False
        x_overlap = not (
            self.bbox.x0 - x_error_margin > other.bbox.x1 + x_error_margin
            or other.bbox.x0 - x_error_margin > self.bbox.x1 + x_error_margin
        )
        y_overlap = not (
            self.bbox.y0 - y_error_margin > other.bbox.y1 + y_error_margin
            or other.bbox.y0 - y_error_margin > self.bbox.y1 + y_error_margin
        )

        return x_overlap and y_overlap

    model_config = ConfigDict(frozen=True)


######################
### TABLE ELEMENTS ###
######################


class TableElement(BaseModel):
    text: str
    bbox: Bbox
    variant: Literal[NodeVariant.TABLE] = NodeVariant.TABLE

    @property
    def area(self) -> float:
        return (self.bbox.x1 - self.bbox.x0) * (self.bbox.y1 - self.bbox.y0)

    @property
    def page(self) -> int:
        return self.bbox.page

    @property
    def tokens(self) -> int:
        return num_tokens(self.text)

    def is_at_similar_height(
        self, other: Union["TableElement", "TextElement"], error_margin: float = 1
    ) -> bool:
        y_distance = abs(self.bbox.y1 - other.bbox.y1)

        return y_distance <= error_margin


#############
### NODES ###
#############


class Node(BaseModel):
    elements: Tuple[Union[TextElement, TableElement], ...]
    _tokenization_lower_limit: int = consts.TOKENIZATION_LOWER_LIMIT
    _tokenization_upper_limit: int = consts.TOKENIZATION_UPPER_LIMIT

    def display(self):
        try:
            from IPython.display import Markdown  # type: ignore
            from IPython.display import display  # type: ignore

            display(Markdown(self.text))
        except ImportError:
            print(self.text)

    @property
    def tokens(self) -> int:
        return sum([e.tokens for e in self.elements])

    @property
    def is_stub(self) -> bool:
        return self.tokens < 50

    @property
    def is_small(self) -> bool:
        return self.tokens < self._tokenization_lower_limit

    @property
    def is_large(self) -> bool:
        return self.tokens > self._tokenization_upper_limit

    @property
    def bbox(self) -> List[Bbox]:
        elements_by_page = defaultdict(list)
        for element in self.elements:
            elements_by_page[element.bbox.page].append(element)

        # Calculate bounding box for each page
        bboxes = []
        for page, elements in elements_by_page.items():
            x0 = min(e.bbox.x0 for e in elements)
            y0 = min(e.bbox.y0 for e in elements)
            x1 = max(e.bbox.x1 for e in elements)
            y1 = max(e.bbox.y1 for e in elements)
            page_height = elements[0].bbox.page_height
            page_width = elements[0].bbox.page_width
            bboxes.append(
                Bbox(
                    page=page,
                    page_height=page_height,
                    page_width=page_width,
                    x0=x0,
                    y0=y0,
                    x1=x1,
                    y1=y1,
                )
            )

        return bboxes

    @property
    def num_pages(self) -> int:
        return len(set(element.bbox.page for element in self.elements))

    @property
    def start_page(self) -> int:
        return min(element.bbox.page for element in self.elements)

    @property
    def end_page(self) -> int:
        return max(element.bbox.page for element in self.elements)

    @property
    def text(self) -> str:
        sorted_elements = sorted(
            self.elements, key=lambda e: (e.page, -e.bbox.y1, e.bbox.x0)
        )

        texts = []
        for i in range(len(sorted_elements)):
            if i > 0 and sorted_elements[i].is_at_similar_height(
                sorted_elements[i - 1]
            ):
                texts.append(" " + sorted_elements[i].text)
            else:
                if i > 0:
                    texts.append("<br>")
                texts.append(sorted_elements[i].text)
        return "".join(texts)

    def overlaps(
        self, other: "Node", x_error_margin: float = 0.0, y_error_margin: float = 0.0
    ) -> bool:
        # Iterate through each bounding box in the current node
        for bbox in self.bbox:
            other_bboxes = [
                other_bbox for other_bbox in other.bbox if other_bbox.page == bbox.page
            ]

            for other_bbox in other_bboxes:
                x_overlap = not (
                    bbox.x0 - x_error_margin > other_bbox.x1 + x_error_margin
                    or other_bbox.x0 - x_error_margin > bbox.x1 + x_error_margin
                )

                y_overlap = not (
                    bbox.y0 - y_error_margin > other_bbox.y1 + y_error_margin
                    or other_bbox.y0 - y_error_margin > bbox.y1 + y_error_margin
                )

                if x_overlap and y_overlap:
                    return True

        return False

    @property
    def aggregate_position(self) -> AggregatePosition:
        """
        Calculate an aggregate position for the node based on its elements.
        Returns a tuple of (min_page, min_y0, min_x0) to use as sort keys.
        """
        min_page = min(element.bbox.page for element in self.elements)
        min_y0 = min(element.bbox.y0 for element in self.elements)
        min_x0 = min(element.bbox.x0 for element in self.elements)
        return AggregatePosition(min_page, min_y0, min_x0)

    def combine(self, other: "Node") -> "Node":
        return Node(elements=self.elements + other.elements)

    model_config = ConfigDict(frozen=True)


################
### DOCUMENT ###
################


class FileMetadata(BaseModel):
    filename: str
    num_pages: int


class ParsedDoc(BaseModel):
    nodes: List["Node"]
    file_metadata: FileMetadata
