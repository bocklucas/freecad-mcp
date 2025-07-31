import json
import logging
import xmlrpc.client
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Any, Literal

from mcp.server.fastmcp import FastMCP, Context
from mcp.types import TextContent, ImageContent

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("FreeCADMCPserver")


_only_text_feedback = False


class FreeCADConnection:
    def __init__(self, host: str = "localhost", port: int = 9875):
        self.server = xmlrpc.client.ServerProxy(f"http://{host}:{port}", allow_none=True)

    def ping(self) -> bool:
        return self.server.ping()

    def create_document(self, name: str) -> dict[str, Any]:
        return self.server.create_document(name)

    def create_object(self, doc_name: str, obj_data: dict[str, Any]) -> dict[str, Any]:
        return self.server.create_object(doc_name, obj_data)

    def edit_object(self, doc_name: str, obj_name: str, obj_data: dict[str, Any]) -> dict[str, Any]:
        return self.server.edit_object(doc_name, obj_name, obj_data)

    def delete_object(self, doc_name: str, obj_name: str) -> dict[str, Any]:
        return self.server.delete_object(doc_name, obj_name)

    def insert_part_from_library(self, relative_path: str) -> dict[str, Any]:
        return self.server.insert_part_from_library(relative_path)

    def execute_code(self, code: str) -> dict[str, Any]:
        return self.server.execute_code(code)

    def get_active_screenshot(self, view_name: str = "Isometric") -> str | None:
        try:
            # Check if we're in a view that supports screenshots
            result = self.server.execute_code("""
import FreeCAD
import FreeCADGui

if FreeCAD.Gui.ActiveDocument and FreeCAD.Gui.ActiveDocument.ActiveView:
    view_type = type(FreeCAD.Gui.ActiveDocument.ActiveView).__name__
    
    # These view types don't support screenshots
    unsupported_views = ['SpreadsheetGui::SheetView', 'DrawingGui::DrawingView', 'TechDrawGui::MDIViewPage']
    
    if view_type in unsupported_views or not hasattr(FreeCAD.Gui.ActiveDocument.ActiveView, 'saveImage'):
        print("Current view does not support screenshots")
        False
    else:
        print(f"Current view supports screenshots: {view_type}")
        True
else:
    print("No active view")
    False
""")

            # If the view doesn't support screenshots, return None
            if not result.get("success", False) or "Current view does not support screenshots" in result.get("message", ""):
                logger.info("Screenshot unavailable in current view (likely Spreadsheet or TechDraw view)")
                return None

            # Otherwise, try to get the screenshot
            return self.server.get_active_screenshot(view_name)
        except Exception as e:
            # Log the error but return None instead of raising an exception
            logger.error(f"Error getting screenshot: {e}")
            return None

    def get_objects(self, doc_name: str) -> list[dict[str, Any]]:
        return self.server.get_objects(doc_name)

    def get_object(self, doc_name: str, obj_name: str) -> dict[str, Any]:
        return self.server.get_object(doc_name, obj_name)

    def get_parts_list(self) -> list[str]:
        return self.server.get_parts_list()


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    try:
        logger.info("FreeCADMCP server starting up")
        try:
            _ = get_freecad_connection()
            logger.info("Successfully connected to FreeCAD on startup")
        except Exception as e:
            logger.warning(f"Could not connect to FreeCAD on startup: {str(e)}")
            logger.warning(
                "Make sure the FreeCAD addon is running before using FreeCAD resources or tools"
            )
        yield {}
    finally:
        # Clean up the global connection on shutdown
        global _freecad_connection
        if _freecad_connection:
            logger.info("Disconnecting from FreeCAD on shutdown")
            _freecad_connection.disconnect()
            _freecad_connection = None
        logger.info("FreeCADMCP server shut down")


mcp = FastMCP(
    "FreeCADMCP",
    description="FreeCAD integration through the Model Context Protocol",
    lifespan=server_lifespan,
)


_freecad_connection: FreeCADConnection | None = None


def get_freecad_connection():
    """Get or create a persistent FreeCAD connection"""
    global _freecad_connection
    if _freecad_connection is None:
        _freecad_connection = FreeCADConnection(host="localhost", port=9875)
        if not _freecad_connection.ping():
            logger.error("Failed to ping FreeCAD")
            _freecad_connection = None
            raise Exception(
                "Failed to connect to FreeCAD. Make sure the FreeCAD addon is running."
            )
    return _freecad_connection


# Helper function to safely add screenshot to response
def add_screenshot_if_available(response, screenshot):
    """Safely add screenshot to response only if it's available"""
    if screenshot is not None and not _only_text_feedback:
        response.append(ImageContent(type="image", data=screenshot, mimeType="image/png"))
    elif not _only_text_feedback:
        # Add an informative message that will be seen by the AI model and user
        response.append(TextContent(
            type="text", 
            text="Note: Visual preview is unavailable in the current view type (such as TechDraw or Spreadsheet). "
                 "Switch to a 3D view to see visual feedback."
        ))
    return response


@mcp.tool()
def create_document(ctx: Context, name: str) -> list[TextContent]:
    """Create a new document in FreeCAD.

    Args:
        name: The name of the document to create.

    Returns:
        A message indicating the success or failure of the document creation.

    Examples:
        If you want to create a document named "MyDocument", you can use the following data.
        ```json
        {
            "name": "MyDocument"
        }
        ```
    """
    freecad = get_freecad_connection()
    try:
        res = freecad.create_document(name)
        if res["success"]:
            return [
                TextContent(type="text", text=f"Document '{res['document_name']}' created successfully")
            ]
        else:
            return [
                TextContent(type="text", text=f"Failed to create document: {res['error']}")
            ]
    except Exception as e:
        logger.error(f"Failed to create document: {str(e)}")
        return [
            TextContent(type="text", text=f"Failed to create document: {str(e)}")
        ]


@mcp.tool()
def create_object(
    ctx: Context,
    doc_name: str,
    obj_type: str,
    obj_name: str,
    analysis_name: str | None = None,
    obj_properties: dict[str, Any] = None,
) -> list[TextContent | ImageContent]:
    """Create a new object in FreeCAD.
    Object type is starts with "Part::" or "Draft::" or "PartDesign::" or "Fem::".

    Args:
        doc_name: The name of the document to create the object in.
        obj_type: The type of the object to create (e.g. 'Part::Box', 'Part::Cylinder', 'Draft::Circle', 'PartDesign::Body', etc.).
        obj_name: The name of the object to create.
        obj_properties: The properties of the object to create.

    Returns:
        A message indicating the success or failure of the object creation and a screenshot of the object.

    Examples:
        If you want to create a cylinder with a height of 30 and a radius of 10, you can use the following data.
        ```json
        {
            "doc_name": "MyCylinder",
            "obj_name": "Cylinder",
            "obj_type": "Part::Cylinder",
            "obj_properties": {
                "Height": 30,
                "Radius": 10,
                "Placement": {
                    "Base": {
                        "x": 10,
                        "y": 10,
                        "z": 0
                    },
                    "Rotation": {
                        "Axis": {
                            "x": 0,
                            "y": 0,
                            "z": 1
                        },
                        "Angle": 45
                    }
                },
                "ViewObject": {
                    "ShapeColor": [0.5, 0.5, 0.5, 1.0]
                }
            }
        }
        ```

        If you want to create a circle with a radius of 10, you can use the following data.
        ```json
        {
            "doc_name": "MyCircle",
            "obj_name": "Circle",
            "obj_type": "Draft::Circle",
        }
        ```

        If you want to create a FEM analysis, you can use the following data.
        ```json
        {
            "doc_name": "MyFEMAnalysis",
            "obj_name": "FemAnalysis",
            "obj_type": "Fem::AnalysisPython",
        }
        ```

        If you want to create a FEM constraint, you can use the following data.
        ```json
        {
            "doc_name": "MyFEMConstraint",
            "obj_name": "FemConstraint",
            "obj_type": "Fem::ConstraintFixed",
            "analysis_name": "MyFEMAnalysis",
            "obj_properties": {
                "References": [
                    {
                        "object_name": "MyObject",
                        "face": "Face1"
                    }
                ]
            }
        }
        ```

        If you want to create a FEM mechanical material, you can use the following data.
        ```json
        {
            "doc_name": "MyFEMAnalysis",
            "obj_name": "FemMechanicalMaterial",
            "obj_type": "Fem::MaterialCommon",
            "analysis_name": "MyFEMAnalysis",
            "obj_properties": {
                "Material": {
                    "Name": "MyMaterial",
                    "Density": "7900 kg/m^3",
                    "YoungModulus": "210 GPa",
                    "PoissonRatio": 0.3
                }
            }
        }
        ```

        If you want to create a FEM mesh, you can use the following data.
        The `Part` property is required.
        ```json
        {
            "doc_name": "MyFEMMesh",
            "obj_name": "FemMesh",
            "obj_type": "Fem::FemMeshGmsh",
            "analysis_name": "MyFEMAnalysis",
            "obj_properties": {
                "Part": "MyObject",
                "ElementSizeMax": 10,
                "ElementSizeMin": 0.1,
                "MeshAlgorithm": 2
            }
        }
        ```
    """
    freecad = get_freecad_connection()
    try:
        obj_data = {"Name": obj_name, "Type": obj_type, "Properties": obj_properties or {}, "Analysis": analysis_name}
        res = freecad.create_object(doc_name, obj_data)
        screenshot = freecad.get_active_screenshot()
        
        if res["success"]:
            response = [
                TextContent(type="text", text=f"Object '{res['object_name']}' created successfully"),
            ]
            return add_screenshot_if_available(response, screenshot)
        else:
            response = [
                TextContent(type="text", text=f"Failed to create object: {res['error']}"),
            ]
            return add_screenshot_if_available(response, screenshot)
    except Exception as e:
        logger.error(f"Failed to create object: {str(e)}")
        return [
            TextContent(type="text", text=f"Failed to create object: {str(e)}")
        ]


@mcp.tool()
def edit_object(
    ctx: Context, doc_name: str, obj_name: str, obj_properties: dict[str, Any]
) -> list[TextContent | ImageContent]:
    """Edit an object in FreeCAD.
    This tool is used when the `create_object` tool cannot handle the object creation.

    Args:
        doc_name: The name of the document to edit the object in.
        obj_name: The name of the object to edit.
        obj_properties: The properties of the object to edit.

    Returns:
        A message indicating the success or failure of the object editing and a screenshot of the object.
    """
    freecad = get_freecad_connection()
    try:
        res = freecad.edit_object(doc_name, obj_name, {"Properties": obj_properties})
        screenshot = freecad.get_active_screenshot()

        if res["success"]:
            response = [
                TextContent(type="text", text=f"Object '{res['object_name']}' edited successfully"),
            ]
            return add_screenshot_if_available(response, screenshot)
        else:
            response = [
                TextContent(type="text", text=f"Failed to edit object: {res['error']}"),
            ]
            return add_screenshot_if_available(response, screenshot)
    except Exception as e:
        logger.error(f"Failed to edit object: {str(e)}")
        return [
            TextContent(type="text", text=f"Failed to edit object: {str(e)}")
        ]


@mcp.tool()
def delete_object(ctx: Context, doc_name: str, obj_name: str) -> list[TextContent | ImageContent]:
    """Delete an object in FreeCAD.

    Args:
        doc_name: The name of the document to delete the object from.
        obj_name: The name of the object to delete.

    Returns:
        A message indicating the success or failure of the object deletion and a screenshot of the object.
    """
    freecad = get_freecad_connection()
    try:
        res = freecad.delete_object(doc_name, obj_name)
        screenshot = freecad.get_active_screenshot()
        
        if res["success"]:
            response = [
                TextContent(type="text", text=f"Object '{res['object_name']}' deleted successfully"),
            ]
            return add_screenshot_if_available(response, screenshot)
        else:
            response = [
                TextContent(type="text", text=f"Failed to delete object: {res['error']}"),
            ]
            return add_screenshot_if_available(response, screenshot)
    except Exception as e:
        logger.error(f"Failed to delete object: {str(e)}")
        return [
            TextContent(type="text", text=f"Failed to delete object: {str(e)}")
        ]


@mcp.tool()
def execute_code(ctx: Context, code: str) -> list[TextContent | ImageContent]:
    """Execute arbitrary Python code in FreeCAD.

    Args:
        code: The Python code to execute.

    Returns:
        A message indicating the success or failure of the code execution, the output of the code execution, and a screenshot of the object.
    """
    freecad = get_freecad_connection()
    try:
        res = freecad.execute_code(code)
        screenshot = freecad.get_active_screenshot()
        
        if res["success"]:
            response = [
                TextContent(type="text", text=f"Code executed successfully: {res['message']}"),
            ]
            return add_screenshot_if_available(response, screenshot)
        else:
            response = [
                TextContent(type="text", text=f"Failed to execute code: {res['error']}"),
            ]
            return add_screenshot_if_available(response, screenshot)
    except Exception as e:
        logger.error(f"Failed to execute code: {str(e)}")
        return [
            TextContent(type="text", text=f"Failed to execute code: {str(e)}")
        ]


@mcp.tool()
def get_view(ctx: Context, view_name: Literal["Isometric", "Front", "Top", "Right", "Back", "Left", "Bottom", "Dimetric", "Trimetric"]) -> list[ImageContent | TextContent]:
    """Get a screenshot of the active view.

    Args:
        view_name: The name of the view to get the screenshot of.
        The following views are available:
        - "Isometric"
        - "Front"
        - "Top"
        - "Right"
        - "Back"
        - "Left"
        - "Bottom"
        - "Dimetric"
        - "Trimetric"

    Returns:
        A screenshot of the active view.
    """
    freecad = get_freecad_connection()
    screenshot = freecad.get_active_screenshot(view_name)
    
    if screenshot is not None:
        return [ImageContent(type="image", data=screenshot, mimeType="image/png")]
    else:
        return [TextContent(type="text", text="Cannot get screenshot in the current view type (such as TechDraw or Spreadsheet)")]


@mcp.tool()
def insert_part_from_library(ctx: Context, relative_path: str) -> list[TextContent | ImageContent]:
    """Insert a part from the parts library addon.

    Args:
        relative_path: The relative path of the part to insert.

    Returns:
        A message indicating the success or failure of the part insertion and a screenshot of the object.
    """
    freecad = get_freecad_connection()
    try:
        res = freecad.insert_part_from_library(relative_path)
        screenshot = freecad.get_active_screenshot()
        
        if res["success"]:
            response = [
                TextContent(type="text", text=f"Part inserted from library: {res['message']}"),
            ]
            return add_screenshot_if_available(response, screenshot)
        else:
            response = [
                TextContent(type="text", text=f"Failed to insert part from library: {res['error']}"),
            ]
            return add_screenshot_if_available(response, screenshot)
    except Exception as e:
        logger.error(f"Failed to insert part from library: {str(e)}")
        return [
            TextContent(type="text", text=f"Failed to insert part from library: {str(e)}")
        ]


@mcp.tool()
def get_objects(ctx: Context, doc_name: str) -> list[dict[str, Any]]:
    """Get all objects in a document.
    You can use this tool to get the objects in a document to see what you can check or edit.

    Args:
        doc_name: The name of the document to get the objects from.

    Returns:
        A list of objects in the document and a screenshot of the document.
    """
    freecad = get_freecad_connection()
    try:
        screenshot = freecad.get_active_screenshot()
        response = [
            TextContent(type="text", text=json.dumps(freecad.get_objects(doc_name))),
        ]
        return add_screenshot_if_available(response, screenshot)
    except Exception as e:
        logger.error(f"Failed to get objects: {str(e)}")
        return [
            TextContent(type="text", text=f"Failed to get objects: {str(e)}")
        ]


@mcp.tool()
def get_object(ctx: Context, doc_name: str, obj_name: str) -> dict[str, Any]:
    """Get an object from a document.
    You can use this tool to get the properties of an object to see what you can check or edit.

    Args:
        doc_name: The name of the document to get the object from.
        obj_name: The name of the object to get.

    Returns:
        The object and a screenshot of the object.
    """
    freecad = get_freecad_connection()
    try:
        screenshot = freecad.get_active_screenshot()
        response = [
            TextContent(type="text", text=json.dumps(freecad.get_object(doc_name, obj_name))),
        ]
        return add_screenshot_if_available(response, screenshot)
    except Exception as e:
        logger.error(f"Failed to get object: {str(e)}")
        return [
            TextContent(type="text", text=f"Failed to get object: {str(e)}")
        ]


@mcp.tool()
def get_parts_list(ctx: Context) -> list[str]:
    """Get the list of parts in the parts library addon.
    """
    freecad = get_freecad_connection()
    parts = freecad.get_parts_list()
    if parts:
        return [
            TextContent(type="text", text=json.dumps(parts))
        ]
    else:
        return [
            TextContent(type="text", text=f"No parts found in the parts library. You must add parts_library addon.")
        ]


@mcp.prompt()
def asset_creation_strategy() -> str:
    return """
Asset Creation Strategy for FreeCAD MCP

When creating content in FreeCAD, always follow these steps:

0. Before starting any task, always use get_objects() to confirm the current state of the document.

1. Utilize the parts library:
   - Check available parts using get_parts_list().
   - If the required part exists in the library, use insert_part_from_library() to insert it into your document.

2. If the appropriate asset is not available in the parts library:
   - Create basic shapes (e.g., cubes, cylinders, spheres) using create_object().
   - Adjust and define detailed properties of the shapes as necessary using edit_object().

3. Always assign clear and descriptive names to objects when adding them to the document.

4. Explicitly set the position, scale, and rotation properties of created or inserted objects using edit_object() to ensure proper spatial relationships.

5. After editing an object, always verify that the set properties have been correctly applied by using get_object().

6. If detailed customization or specialized operations are necessary, use execute_code() to run custom Python scripts.

Only revert to basic creation methods in the following cases:
- When the required asset is not available in the parts library.
- When a basic shape is explicitly requested.
- When creating complex shapes requires custom scripting.
"""


@mcp.tool()
def create_sketch(
    ctx: Context,
    doc_name: str,
    sketch_name: str,
    plane: Literal["XY", "XZ", "YZ"] = "XY",
    body_name: str | None = None
) -> list[TextContent | ImageContent]:
    """Create a new sketch in FreeCAD.

    Args:
        doc_name: The name of the document to create the sketch in.
        sketch_name: The name of the sketch to create.
        plane: The plane to create the sketch on ("XY", "XZ", or "YZ").
        body_name: Optional name of the PartDesign Body to create the sketch in.

    Returns:
        A message indicating the success or failure of the sketch creation and a screenshot.

    Examples:
        Create a sketch on the XY plane:
        ```json
        {
            "doc_name": "MyDocument",
            "sketch_name": "Sketch001",
            "plane": "XY"
        }
        ```

        Create a sketch in a PartDesign Body:
        ```json
        {
            "doc_name": "MyDocument",
            "sketch_name": "Sketch001",
            "plane": "XY",
            "body_name": "Body"
        }
        ```
    """
    freecad = get_freecad_connection()
    try:
        if body_name:
            # Create sketch in a PartDesign Body
            code = f"""
import FreeCAD
import PartDesign

doc = FreeCAD.getDocument('{doc_name}')
if '{body_name}' in [obj.Name for obj in doc.Objects]:
    body = doc.getObject('{body_name}')
else:
    body = doc.addObject('PartDesign::Body', '{body_name}')

sketch = body.newObject('Sketcher::SketchObject', '{sketch_name}')

# Set the sketch plane
if '{plane}' == 'XY':
    sketch.Support = (doc.getObject('XY_Plane'), [''])
    sketch.MapMode = 'FlatFace'
elif '{plane}' == 'XZ':
    sketch.Support = (doc.getObject('XZ_Plane'), [''])
    sketch.MapMode = 'FlatFace'
elif '{plane}' == 'YZ':
    sketch.Support = (doc.getObject('YZ_Plane'), [''])
    sketch.MapMode = 'FlatFace'

doc.recompute()
FreeCAD.Gui.ActiveDocument.setEdit(sketch.Name)
"""
        else:
            # Create standalone sketch
            code = f"""
import FreeCAD
import Sketcher

doc = FreeCAD.getDocument('{doc_name}')
sketch = doc.addObject('Sketcher::SketchObject', '{sketch_name}')

# Set the sketch plane
import FreeCAD
if '{plane}' == 'XY':
    sketch.Placement = FreeCAD.Placement(FreeCAD.Vector(0,0,0), FreeCAD.Rotation(0,0,0,1))
elif '{plane}' == 'XZ':
    sketch.Placement = FreeCAD.Placement(FreeCAD.Vector(0,0,0), FreeCAD.Rotation(1,0,0,90))
elif '{plane}' == 'YZ':
    sketch.Placement = FreeCAD.Placement(FreeCAD.Vector(0,0,0), FreeCAD.Rotation(0,1,0,90))

doc.recompute()
FreeCAD.Gui.ActiveDocument.setEdit(sketch.Name)
"""

        res = freecad.execute_code(code)
        screenshot = freecad.get_active_screenshot()
        
        if res["success"]:
            response = [
                TextContent(type="text", text=f"Sketch '{sketch_name}' created successfully on {plane} plane"),
            ]
            return add_screenshot_if_available(response, screenshot)
        else:
            response = [
                TextContent(type="text", text=f"Failed to create sketch: {res['error']}"),
            ]
            return add_screenshot_if_available(response, screenshot)
    except Exception as e:
        logger.error(f"Failed to create sketch: {str(e)}")
        return [
            TextContent(type="text", text=f"Failed to create sketch: {str(e)}")
        ]


@mcp.tool()
def sketch_add_point(
    ctx: Context,
    doc_name: str,
    sketch_name: str,
    x: float,
    y: float
) -> list[TextContent | ImageContent]:
    """Add a point to a sketch.

    Args:
        doc_name: The name of the document containing the sketch.
        sketch_name: The name of the sketch to add the point to.
        x: The X coordinate of the point.
        y: The Y coordinate of the point.

    Returns:
        A message indicating the success or failure and a screenshot.

    Examples:
        Add a point at coordinates (10, 5):
        ```json
        {
            "doc_name": "MyDocument",
            "sketch_name": "Sketch001",
            "x": 10,
            "y": 5
        }
        ```
    """
    freecad = get_freecad_connection()
    try:
        code = f"""
import FreeCAD

doc = FreeCAD.getDocument('{doc_name}')
sketch = doc.getObject('{sketch_name}')
if sketch:
    sketch.addGeometry(Part.Point(FreeCAD.Vector({x}, {y}, 0)))
    doc.recompute()
    print(f"Point added at ({x}, {y})")
else:
    print("Sketch not found")
"""
        res = freecad.execute_code(code)
        screenshot = freecad.get_active_screenshot()
        
        if res["success"]:
            response = [
                TextContent(type="text", text=f"Point added to sketch '{sketch_name}' at ({x}, {y})"),
            ]
            return add_screenshot_if_available(response, screenshot)
        else:
            response = [
                TextContent(type="text", text=f"Failed to add point: {res['error']}"),
            ]
            return add_screenshot_if_available(response, screenshot)
    except Exception as e:
        logger.error(f"Failed to add point: {str(e)}")
        return [
            TextContent(type="text", text=f"Failed to add point: {str(e)}")
        ]


@mcp.tool()
def sketch_add_line(
    ctx: Context,
    doc_name: str,
    sketch_name: str,
    x1: float,
    y1: float,
    x2: float,
    y2: float
) -> list[TextContent | ImageContent]:
    """Add a line to a sketch.

    Args:
        doc_name: The name of the document containing the sketch.
        sketch_name: The name of the sketch to add the line to.
        x1: The X coordinate of the start point.
        y1: The Y coordinate of the start point. 
        x2: The X coordinate of the end point.
        y2: The Y coordinate of the end point.

    Returns:
        A message indicating the success or failure and a screenshot.

    Examples:
        Add a line from (0, 0) to (10, 10):
        ```json
        {
            "doc_name": "MyDocument",
            "sketch_name": "Sketch001",
            "x1": 0,
            "y1": 0,
            "x2": 10,
            "y2": 10
        }
        ```
    """
    freecad = get_freecad_connection()
    try:
        code = f"""
import FreeCAD
import Part

doc = FreeCAD.getDocument('{doc_name}')
sketch = doc.getObject('{sketch_name}')
if sketch:
    line = Part.LineSegment(FreeCAD.Vector({x1}, {y1}, 0), FreeCAD.Vector({x2}, {y2}, 0))
    sketch.addGeometry(line)
    doc.recompute()
    print(f"Line added from ({x1}, {y1}) to ({x2}, {y2})")
else:
    print("Sketch not found")
"""
        res = freecad.execute_code(code)
        screenshot = freecad.get_active_screenshot()
        
        if res["success"]:
            response = [
                TextContent(type="text", text=f"Line added to sketch '{sketch_name}' from ({x1}, {y1}) to ({x2}, {y2})"),
            ]
            return add_screenshot_if_available(response, screenshot)
        else:
            response = [
                TextContent(type="text", text=f"Failed to add line: {res['error']}"),
            ]
            return add_screenshot_if_available(response, screenshot)
    except Exception as e:
        logger.error(f"Failed to add line: {str(e)}")
        return [
            TextContent(type="text", text=f"Failed to add line: {str(e)}")
        ]


@mcp.tool()
def sketch_add_circle(
    ctx: Context,
    doc_name: str,
    sketch_name: str,
    center_x: float,
    center_y: float,
    radius: float
) -> list[TextContent | ImageContent]:
    """Add a circle to a sketch.

    Args:
        doc_name: The name of the document containing the sketch.
        sketch_name: The name of the sketch to add the circle to.
        center_x: The X coordinate of the circle center.
        center_y: The Y coordinate of the circle center.
        radius: The radius of the circle.

    Returns:
        A message indicating the success or failure and a screenshot.

    Examples:
        Add a circle with center at (5, 5) and radius 10:
        ```json
        {
            "doc_name": "MyDocument",
            "sketch_name": "Sketch001",
            "center_x": 5,
            "center_y": 5,
            "radius": 10
        }
        ```
    """
    freecad = get_freecad_connection()
    try:
        code = f"""
import FreeCAD
import Part

doc = FreeCAD.getDocument('{doc_name}')
sketch = doc.getObject('{sketch_name}')
if sketch:
    circle = Part.Circle(FreeCAD.Vector({center_x}, {center_y}, 0), FreeCAD.Vector(0, 0, 1), {radius})
    sketch.addGeometry(circle)
    doc.recompute()
    print(f"Circle added with center at ({center_x}, {center_y}) and radius {radius}")
else:
    print("Sketch not found")
"""
        res = freecad.execute_code(code)
        screenshot = freecad.get_active_screenshot()
        
        if res["success"]:
            response = [
                TextContent(type="text", text=f"Circle added to sketch '{sketch_name}' with center at ({center_x}, {center_y}) and radius {radius}"),
            ]
            return add_screenshot_if_available(response, screenshot)
        else:
            response = [
                TextContent(type="text", text=f"Failed to add circle: {res['error']}"),
            ]
            return add_screenshot_if_available(response, screenshot)
    except Exception as e:
        logger.error(f"Failed to add circle: {str(e)}")
        return [
            TextContent(type="text", text=f"Failed to add circle: {str(e)}")
        ]


@mcp.tool()
def sketch_add_arc(
    ctx: Context,
    doc_name: str,
    sketch_name: str,
    center_x: float,
    center_y: float,
    radius: float,
    start_angle: float,
    end_angle: float
) -> list[TextContent | ImageContent]:
    """Add a circular arc to a sketch.

    Args:
        doc_name: The name of the document containing the sketch.
        sketch_name: The name of the sketch to add the arc to.
        center_x: The X coordinate of the arc center.
        center_y: The Y coordinate of the arc center.
        radius: The radius of the arc.
        start_angle: The start angle in degrees.
        end_angle: The end angle in degrees.

    Returns:
        A message indicating the success or failure and a screenshot.

    Examples:
        Add a 90-degree arc from 0 to 90 degrees:
        ```json
        {
            "doc_name": "MyDocument",
            "sketch_name": "Sketch001",
            "center_x": 0,
            "center_y": 0,
            "radius": 20,
            "start_angle": 0,
            "end_angle": 90
        }
        ```
    """
    freecad = get_freecad_connection()
    try:
        code = f"""
import FreeCAD
import Part
import math

doc = FreeCAD.getDocument('{doc_name}')
sketch = doc.getObject('{sketch_name}')
if sketch:
    # Convert degrees to radians
    start_rad = math.radians({start_angle})
    end_rad = math.radians({end_angle})
    
    # Create arc
    arc = Part.ArcOfCircle(
        Part.Circle(FreeCAD.Vector({center_x}, {center_y}, 0), FreeCAD.Vector(0, 0, 1), {radius}),
        start_rad,
        end_rad
    )
    sketch.addGeometry(arc)
    doc.recompute()
    print(f"Arc added with center at ({center_x}, {center_y}), radius {radius}, from {start_angle}° to {end_angle}°")
else:
    print("Sketch not found")
"""
        res = freecad.execute_code(code)
        screenshot = freecad.get_active_screenshot()
        
        if res["success"]:
            response = [
                TextContent(type="text", text=f"Arc added to sketch '{sketch_name}' with center at ({center_x}, {center_y}), radius {radius}, from {start_angle}° to {end_angle}°"),
            ]
            return add_screenshot_if_available(response, screenshot)
        else:
            response = [
                TextContent(type="text", text=f"Failed to add arc: {res['error']}"),
            ]
            return add_screenshot_if_available(response, screenshot)
    except Exception as e:
        logger.error(f"Failed to add arc: {str(e)}")
        return [
            TextContent(type="text", text=f"Failed to add arc: {str(e)}")
        ]


@mcp.tool()
def sketch_add_rectangle(
    ctx: Context,
    doc_name: str,
    sketch_name: str,
    x1: float,
    y1: float,
    x2: float,
    y2: float
) -> list[TextContent | ImageContent]:
    """Add a rectangle to a sketch.

    Args:
        doc_name: The name of the document containing the sketch.
        sketch_name: The name of the sketch to add the rectangle to.
        x1: The X coordinate of the first corner.
        y1: The Y coordinate of the first corner.
        x2: The X coordinate of the opposite corner.
        y2: The Y coordinate of the opposite corner.

    Returns:
        A message indicating the success or failure and a screenshot.

    Examples:
        Add a rectangle from (0, 0) to (20, 10):
        ```json
        {
            "doc_name": "MyDocument",
            "sketch_name": "Sketch001",
            "x1": 0,
            "y1": 0,
            "x2": 20,
            "y2": 10
        }
        ```
    """
    freecad = get_freecad_connection()
    try:
        code = f"""
import FreeCAD
import Part

doc = FreeCAD.getDocument('{doc_name}')
sketch = doc.getObject('{sketch_name}')
if sketch:
    # Add four lines to form a rectangle
    line1 = Part.LineSegment(FreeCAD.Vector({x1}, {y1}, 0), FreeCAD.Vector({x2}, {y1}, 0))
    line2 = Part.LineSegment(FreeCAD.Vector({x2}, {y1}, 0), FreeCAD.Vector({x2}, {y2}, 0))
    line3 = Part.LineSegment(FreeCAD.Vector({x2}, {y2}, 0), FreeCAD.Vector({x1}, {y2}, 0))
    line4 = Part.LineSegment(FreeCAD.Vector({x1}, {y2}, 0), FreeCAD.Vector({x1}, {y1}, 0))
    
    geo1 = sketch.addGeometry(line1)
    geo2 = sketch.addGeometry(line2)
    geo3 = sketch.addGeometry(line3)
    geo4 = sketch.addGeometry(line4)
    
    # Add coincident constraints to connect the lines
    sketch.addConstraint(Sketcher.Constraint('Coincident', geo1, 2, geo2, 1))
    sketch.addConstraint(Sketcher.Constraint('Coincident', geo2, 2, geo3, 1))
    sketch.addConstraint(Sketcher.Constraint('Coincident', geo3, 2, geo4, 1))
    sketch.addConstraint(Sketcher.Constraint('Coincident', geo4, 2, geo1, 1))
    
    # Add perpendicular constraints
    sketch.addConstraint(Sketcher.Constraint('Perpendicular', geo1, geo2))
    sketch.addConstraint(Sketcher.Constraint('Perpendicular', geo2, geo3))
    sketch.addConstraint(Sketcher.Constraint('Perpendicular', geo3, geo4))
    sketch.addConstraint(Sketcher.Constraint('Perpendicular', geo4, geo1))
    
    doc.recompute()
    print(f"Rectangle added from ({x1}, {y1}) to ({x2}, {y2})")
else:
    print("Sketch not found")
"""
        res = freecad.execute_code(code)
        screenshot = freecad.get_active_screenshot()
        
        if res["success"]:
            response = [
                TextContent(type="text", text=f"Rectangle added to sketch '{sketch_name}' from ({x1}, {y1}) to ({x2}, {y2})"),
            ]
            return add_screenshot_if_available(response, screenshot)
        else:
            response = [
                TextContent(type="text", text=f"Failed to add rectangle: {res['error']}"),
            ]
            return add_screenshot_if_available(response, screenshot)
    except Exception as e:
        logger.error(f"Failed to add rectangle: {str(e)}")
        return [
            TextContent(type="text", text=f"Failed to add rectangle: {str(e)}")
        ]


@mcp.tool()
def sketch_add_polyline(
    ctx: Context,
    doc_name: str,
    sketch_name: str,
    points: list[dict[str, float]],
    closed: bool = False
) -> list[TextContent | ImageContent]:
    """Add a polyline (connected line segments) to a sketch.

    Args:
        doc_name: The name of the document containing the sketch.
        sketch_name: The name of the sketch to add the polyline to.
        points: List of points, each with 'x' and 'y' coordinates.
        closed: Whether to close the polyline by connecting the last point to the first.

    Returns:
        A message indicating the success or failure and a screenshot.

    Examples:
        Add an open polyline with 4 points:
        ```json
        {
            "doc_name": "MyDocument",
            "sketch_name": "Sketch001",
            "points": [
                {"x": 0, "y": 0},
                {"x": 10, "y": 0},
                {"x": 15, "y": 10},
                {"x": 5, "y": 15}
            ],
            "closed": false
        }
        ```

        Add a closed polygon:
        ```json
        {
            "doc_name": "MyDocument",
            "sketch_name": "Sketch001",
            "points": [
                {"x": 0, "y": 0},
                {"x": 20, "y": 0},
                {"x": 10, "y": 15}
            ],
            "closed": true
        }
        ```
    """
    freecad = get_freecad_connection()
    try:
        # Convert points list to string representation for the code
        points_str = str([(p['x'], p['y']) for p in points])
        
        code = f"""
import FreeCAD
import Part
import Sketcher

doc = FreeCAD.getDocument('{doc_name}')
sketch = doc.getObject('{sketch_name}')
if sketch:
    points = {points_str}
    
    if len(points) < 2:
        print("Need at least 2 points for a polyline")
    else:
        geo_indices = []
        
        # Add line segments
        for i in range(len(points) - 1):
            x1, y1 = points[i]
            x2, y2 = points[i + 1]
            line = Part.LineSegment(FreeCAD.Vector(x1, y1, 0), FreeCAD.Vector(x2, y2, 0))
            geo_idx = sketch.addGeometry(line)
            geo_indices.append(geo_idx)
        
        # If closed, add line from last to first point
        if {str(closed).lower()}:
            x1, y1 = points[-1]
            x2, y2 = points[0]
            line = Part.LineSegment(FreeCAD.Vector(x1, y1, 0), FreeCAD.Vector(x2, y2, 0))
            geo_idx = sketch.addGeometry(line)
            geo_indices.append(geo_idx)
        
        # Add coincident constraints to connect the lines
        for i in range(len(geo_indices) - 1):
            sketch.addConstraint(Sketcher.Constraint('Coincident', geo_indices[i], 2, geo_indices[i+1], 1))
        
        # If closed, connect last line to first line
        if {str(closed).lower()} and len(geo_indices) > 2:
            sketch.addConstraint(Sketcher.Constraint('Coincident', geo_indices[-1], 2, geo_indices[0], 1))
        
        doc.recompute()
        print(f"Polyline added with {{len(points)}} points, closed: {closed}")
else:
    print("Sketch not found")
"""
        res = freecad.execute_code(code)
        screenshot = freecad.get_active_screenshot()
        
        if res["success"]:
            response = [
                TextContent(type="text", text=f"Polyline added to sketch '{sketch_name}' with {len(points)} points, closed: {closed}"),
            ]
            return add_screenshot_if_available(response, screenshot)
        else:
            response = [
                TextContent(type="text", text=f"Failed to add polyline: {res['error']}"),
            ]
            return add_screenshot_if_available(response, screenshot)
    except Exception as e:
        logger.error(f"Failed to add polyline: {str(e)}")
        return [
            TextContent(type="text", text=f"Failed to add polyline: {str(e)}")
        ]


@mcp.tool()
def sketch_add_ellipse(
    ctx: Context,
    doc_name: str,
    sketch_name: str,
    center_x: float,
    center_y: float,
    major_radius: float,
    minor_radius: float,
    angle: float = 0
) -> list[TextContent | ImageContent]:
    """Add an ellipse to a sketch.

    Args:
        doc_name: The name of the document containing the sketch.
        sketch_name: The name of the sketch to add the ellipse to.
        center_x: The X coordinate of the ellipse center.
        center_y: The Y coordinate of the ellipse center.
        major_radius: The major radius of the ellipse.
        minor_radius: The minor radius of the ellipse.
        angle: The rotation angle of the ellipse in degrees (default: 0).

    Returns:
        A message indicating the success or failure and a screenshot.

    Examples:
        Add an ellipse with center at (0, 0), major radius 20, minor radius 10:
        ```json
        {
            "doc_name": "MyDocument",
            "sketch_name": "Sketch001",
            "center_x": 0,
            "center_y": 0,
            "major_radius": 20,
            "minor_radius": 10,
            "angle": 0
        }
        ```
    """
    freecad = get_freecad_connection()
    try:
        code = f"""
import FreeCAD
import Part
import math

doc = FreeCAD.getDocument('{doc_name}')
sketch = doc.getObject('{sketch_name}')
if sketch:
    # Create ellipse
    center = FreeCAD.Vector({center_x}, {center_y}, 0)
    
    # Calculate major axis direction based on angle
    angle_rad = math.radians({angle})
    major_axis = FreeCAD.Vector(math.cos(angle_rad), math.sin(angle_rad), 0)
    
    ellipse = Part.Ellipse(center, {major_radius}, {minor_radius})
    ellipse.MajorRadius = {major_radius}
    ellipse.MinorRadius = {minor_radius}
    ellipse.Center = center
    
    sketch.addGeometry(ellipse)
    doc.recompute()
    print(f"Ellipse added with center at ({center_x}, {center_y}), major radius {major_radius}, minor radius {minor_radius}")
else:
    print("Sketch not found")
"""
        res = freecad.execute_code(code)
        screenshot = freecad.get_active_screenshot()
        
        if res["success"]:
            response = [
                TextContent(type="text", text=f"Ellipse added to sketch '{sketch_name}' with center at ({center_x}, {center_y}), major radius {major_radius}, minor radius {minor_radius}"),
            ]
            return add_screenshot_if_available(response, screenshot)
        else:
            response = [
                TextContent(type="text", text=f"Failed to add ellipse: {res['error']}"),
            ]
            return add_screenshot_if_available(response, screenshot)
    except Exception as e:
        logger.error(f"Failed to add ellipse: {str(e)}")
        return [
            TextContent(type="text", text=f"Failed to add ellipse: {str(e)}")
        ]


@mcp.tool()
def sketch_add_regular_polygon(
    ctx: Context,
    doc_name: str,
    sketch_name: str,
    center_x: float,
    center_y: float,
    radius: float,
    sides: int,
    angle: float = 0
) -> list[TextContent | ImageContent]:
    """Add a regular polygon to a sketch.

    Args:
        doc_name: The name of the document containing the sketch.
        sketch_name: The name of the sketch to add the polygon to.
        center_x: The X coordinate of the polygon center.
        center_y: The Y coordinate of the polygon center.
        radius: The radius (distance from center to vertex).
        sides: The number of sides (must be >= 3).
        angle: The rotation angle in degrees (default: 0).

    Returns:
        A message indicating the success or failure and a screenshot.

    Examples:
        Add a hexagon (6-sided polygon):
        ```json
        {
            "doc_name": "MyDocument",
            "sketch_name": "Sketch001",
            "center_x": 0,
            "center_y": 0,
            "radius": 15,
            "sides": 6,
            "angle": 0
        }
        ```

        Add a rotated pentagon:
        ```json
        {
            "doc_name": "MyDocument",
            "sketch_name": "Sketch001",
            "center_x": 10,
            "center_y": 10,
            "radius": 20,
            "sides": 5,
            "angle": 36
        }
        ```
    """
    freecad = get_freecad_connection()
    try:
        code = f"""
import FreeCAD
import Part
import math
import Sketcher

doc = FreeCAD.getDocument('{doc_name}')
sketch = doc.getObject('{sketch_name}')
if sketch and {sides} >= 3:
    center_x, center_y = {center_x}, {center_y}
    radius = {radius}
    sides = {sides}
    angle_offset = math.radians({angle})
    
    # Calculate polygon vertices
    points = []
    for i in range(sides):
        angle = (2 * math.pi * i / sides) + angle_offset
        x = center_x + radius * math.cos(angle)
        y = center_y + radius * math.sin(angle)
        points.append((x, y))
    
    # Add lines for each side
    geo_indices = []
    for i in range(sides):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % sides]  # Wrap around to first point
        line = Part.LineSegment(FreeCAD.Vector(x1, y1, 0), FreeCAD.Vector(x2, y2, 0))
        geo_idx = sketch.addGeometry(line)
        geo_indices.append(geo_idx)
    
    # Add coincident constraints to connect the lines
    for i in range(sides):
        next_i = (i + 1) % sides
        sketch.addConstraint(Sketcher.Constraint('Coincident', geo_indices[i], 2, geo_indices[next_i], 1))
    
    doc.recompute()
    print(f"Regular {sides}-sided polygon added with center at ({center_x}, {center_y}) and radius {radius}")
elif {sides} < 3:
    print("Polygon must have at least 3 sides")
else:
    print("Sketch not found")
"""
        res = freecad.execute_code(code)
        screenshot = freecad.get_active_screenshot()
        
        if res["success"]:
            response = [
                TextContent(type="text", text=f"Regular {sides}-sided polygon added to sketch '{sketch_name}' with center at ({center_x}, {center_y}) and radius {radius}"),
            ]
            return add_screenshot_if_available(response, screenshot)
        else:
            response = [
                TextContent(type="text", text=f"Failed to add polygon: {res['error']}"),
            ]
            return add_screenshot_if_available(response, screenshot)
    except Exception as e:
        logger.error(f"Failed to add polygon: {str(e)}")
        return [
            TextContent(type="text", text=f"Failed to add polygon: {str(e)}")
        ]


@mcp.tool()
def sketch_add_constraint(
    ctx: Context,
    doc_name: str,
    sketch_name: str,
    constraint_type: Literal["Horizontal", "Vertical", "Parallel", "Perpendicular", "Tangent", "Equal", "Symmetric", "Distance", "Radius", "Angle", "Coincident", "PointOnObject"],
    geometry_indices: list[int],
    value: float | None = None,
    point_indices: list[int] | None = None
) -> list[TextContent | ImageContent]:
    """Add a constraint to a sketch.

    Args:
        doc_name: The name of the document containing the sketch.
        sketch_name: The name of the sketch to add the constraint to.
        constraint_type: The type of constraint to add.
        geometry_indices: List of geometry indices that the constraint applies to.
        value: Optional numeric value for constraints like Distance, Radius, Angle.
        point_indices: Optional list of point indices for constraints that apply to specific points.

    Returns:
        A message indicating the success or failure and a screenshot.

    Examples:
        Add a horizontal constraint to geometry 0:
        ```json
        {
            "doc_name": "MyDocument",
            "sketch_name": "Sketch001",
            "constraint_type": "Horizontal",
            "geometry_indices": [0]
        }
        ```

        Add a distance constraint between two points:
        ```json
        {
            "doc_name": "MyDocument",
            "sketch_name": "Sketch001",
            "constraint_type": "Distance",
            "geometry_indices": [0, 1],
            "point_indices": [2, 1],
            "value": 25.0
        }
        ```
    """
    freecad = get_freecad_connection()
    try:
        geo_indices_str = str(geometry_indices)
        point_indices_str = str(point_indices) if point_indices else "None"
        
        code = f"""
import FreeCAD
import Sketcher

doc = FreeCAD.getDocument('{doc_name}')
sketch = doc.getObject('{sketch_name}')
if sketch:
    constraint_type = '{constraint_type}'
    geo_indices = {geo_indices_str}
    point_indices = {point_indices_str}
    value = {value}
    
    try:
        if constraint_type in ['Horizontal', 'Vertical'] and len(geo_indices) == 1:
            sketch.addConstraint(Sketcher.Constraint(constraint_type, geo_indices[0]))
        elif constraint_type in ['Parallel', 'Perpendicular', 'Tangent', 'Equal'] and len(geo_indices) == 2:
            sketch.addConstraint(Sketcher.Constraint(constraint_type, geo_indices[0], geo_indices[1]))
        elif constraint_type == 'Coincident' and len(geo_indices) == 2 and point_indices and len(point_indices) == 2:
            sketch.addConstraint(Sketcher.Constraint(constraint_type, geo_indices[0], point_indices[0], geo_indices[1], point_indices[1]))
        elif constraint_type == 'Distance' and len(geo_indices) == 2 and point_indices and len(point_indices) == 2 and value is not None:
            sketch.addConstraint(Sketcher.Constraint(constraint_type, geo_indices[0], point_indices[0], geo_indices[1], point_indices[1], value))
        elif constraint_type == 'Radius' and len(geo_indices) == 1 and value is not None:
            sketch.addConstraint(Sketcher.Constraint(constraint_type, geo_indices[0], value))
        elif constraint_type == 'Angle' and len(geo_indices) == 2 and value is not None:
            sketch.addConstraint(Sketcher.Constraint(constraint_type, geo_indices[0], geo_indices[1], math.radians(value)))
        else:
            print(f"Invalid constraint parameters for {{constraint_type}}")
            
        doc.recompute()
        print(f"{{constraint_type}} constraint added successfully")
    except Exception as e:
        print(f"Error adding constraint: {{e}}")
else:
    print("Sketch not found")
"""
        res = freecad.execute_code(code)
        screenshot = freecad.get_active_screenshot()
        
        if res["success"]:
            response = [
                TextContent(type="text", text=f"{constraint_type} constraint added to sketch '{sketch_name}'"),
            ]
            return add_screenshot_if_available(response, screenshot)
        else:
            response = [
                TextContent(type="text", text=f"Failed to add constraint: {res['error']}"),
            ]
            return add_screenshot_if_available(response, screenshot)
    except Exception as e:
        logger.error(f"Failed to add constraint: {str(e)}")
        return [
            TextContent(type="text", text=f"Failed to add constraint: {str(e)}")
        ]


@mcp.tool()
def sketch_close_edit(
    ctx: Context,
    doc_name: str,
    sketch_name: str
) -> list[TextContent | ImageContent]:
    """Close sketch editing mode and return to normal view.

    Args:
        doc_name: The name of the document containing the sketch.
        sketch_name: The name of the sketch to close editing for.

    Returns:
        A message indicating the success or failure and a screenshot.

    Examples:
        Close editing mode for a sketch:
        ```json
        {
            "doc_name": "MyDocument",
            "sketch_name": "Sketch001"
        }
        ```
    """
    freecad = get_freecad_connection()
    try:
        code = f"""
import FreeCAD
import FreeCADGui

doc = FreeCAD.getDocument('{doc_name}')
sketch = doc.getObject('{sketch_name}')
if sketch:
    FreeCADGui.ActiveDocument.resetEdit()
    doc.recompute()
    print(f"Closed editing mode for sketch '{sketch_name}'")
else:
    print("Sketch not found")
"""
        res = freecad.execute_code(code)
        screenshot = freecad.get_active_screenshot()
        
        if res["success"]:
            response = [
                TextContent(type="text", text=f"Closed editing mode for sketch '{sketch_name}'"),
            ]
            return add_screenshot_if_available(response, screenshot)
        else:
            response = [
                TextContent(type="text", text=f"Failed to close sketch editing: {res['error']}"),
            ]
            return add_screenshot_if_available(response, screenshot)
    except Exception as e:
        logger.error(f"Failed to close sketch editing: {str(e)}")
        return [
            TextContent(type="text", text=f"Failed to close sketch editing: {str(e)}")
        ]


def main():
    """Run the MCP server"""
    global _only_text_feedback
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--only-text-feedback", action="store_true", help="Only return text feedback")
    args = parser.parse_args()
    _only_text_feedback = args.only_text_feedback
    logger.info(f"Only text feedback: {_only_text_feedback}")
    mcp.run()