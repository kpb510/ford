# -*- coding: utf-8 -*-
#
#  graphs.py
#  This file is part of FORD.
#
#  Copyright 2015 Christopher MacMackin <cmacmackin@gmail.com>
#
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#  MA 02110-1301, USA.
#
#

import colorsys
import copy
import itertools
import os
import pathlib
import re
import shutil
import warnings

from graphviz import Digraph, ExecutableNotFound
from graphviz import version as graphviz_version
from tqdm import tqdm
from tqdm.contrib.concurrent import process_map

from ford.sourceform import (
    ExternalFunction,
    ExternalInterface,
    ExternalModule,
    ExternalProgram,
    ExternalSourceFile,
    ExternalSubmodule,
    ExternalSubroutine,
    ExternalType,
    FortranBlockData,
    FortranFunction,
    FortranInterface,
    FortranModule,
    FortranProgram,
    FortranSourceFile,
    FortranSubmodule,
    FortranSubmoduleProcedure,
    FortranSubroutine,
    FortranType,
)

try:
    graphviz_version()
    graphviz_installed = True
except ExecutableNotFound:
    graphviz_installed = False

_coloured_edges = False


def set_coloured_edges(val):
    """
    Public accessor to set whether to use coloured edges in graph or just
    use black ones.
    """
    global _coloured_edges
    _coloured_edges = val


_parentdir = ""


def set_graphs_parentdir(val):
    """
    Public accessor to set the parent directory of the graphs.
    Needed for relative paths.
    """
    global _parentdir
    _parentdir = val


def rainbowcolour(depth, maxd):
    if not _coloured_edges:
        return "#000000"
    (r, g, b) = colorsys.hsv_to_rgb(float(depth) / maxd, 1.0, 1.0)
    return f"#{int(255 * r)}{int(255 * g)}{int(255 * b)}"


HYPERLINK_RE = re.compile(
    r"^\s*<\s*a\s+.*href=(\"[^\"]+\"|'[^']+').*>(.*)</\s*a\s*>\s*$", re.IGNORECASE
)
WIDTH_RE = re.compile('width="(.*?)pt"', re.IGNORECASE)
HEIGHT_RE = re.compile('height="(.*?)pt"', re.IGNORECASE)
EM_RE = re.compile("<em>(.*)</em>", re.IGNORECASE)


def newdict(old, key, val):
    new = copy.copy(old)
    new[key] = val
    return new


def is_module(obj):
    return isinstance(obj, FortranModule)


def is_submodule(obj):
    return isinstance(obj, FortranSubmodule)


def is_type(obj):
    return isinstance(obj, FortranType)


def is_proc(obj):
    return isinstance(
        obj,
        (
            FortranFunction,
            FortranSubroutine,
            FortranInterface,
            FortranSubmoduleProcedure,
        ),
    )


def is_program(obj):
    return isinstance(obj, FortranProgram)


def is_sourcefile(obj):
    return isinstance(obj, FortranSourceFile)


def is_blockdata(obj):
    return isinstance(obj, FortranBlockData)


class GraphData(object):
    """
    Contains all of the nodes which may be displayed on a graph.
    """

    def __init__(self):
        self.submodules = {}
        self.modules = {}
        self.types = {}
        self.procedures = {}
        self.programs = {}
        self.sourcefiles = {}
        self.blockdata = {}

    def _get_collection_and_node_type(self, obj):
        if is_submodule(obj):
            return self.submodules, SubmodNode
        if is_module(obj):
            return self.modules, ModNode
        if is_type(obj):
            return self.types, TypeNode
        if is_proc(obj):
            return self.procedures, ProcNode
        if is_program(obj):
            return self.programs, ProgNode
        if is_sourcefile(obj):
            return self.sourcefiles, FileNode
        if is_blockdata(obj):
            return self.blockdata, BlockNode

        raise BadType(
            f"Unrecognised object type '{type(obj).__name__}' when constructing graphs"
        )

    def register(self, obj, hist=None):
        """
        Takes a FortranObject and adds it to the appropriate list, if
        not already present.
        """

        collection, NodeType = self._get_collection_and_node_type(obj)
        if obj not in collection:
            collection[obj] = NodeType(obj, self, hist)

    def get_node(self, obj, hist=None):
        """
        Returns the node corresponding to obj. If does not already exist
        then it will create it.
        """

        collection, _ = self._get_collection_and_node_type(obj)
        if obj not in collection:
            self.register(obj, hist)

        return collection[obj]


class BaseNode:
    colour = "#777777"

    def __init__(self, obj):
        self.attribs = {"color": self.colour, "fontcolor": "white", "style": "filled"}
        self.fromstr = type(obj) is str
        if isinstance(
            obj,
            (
                ExternalModule,
                ExternalSubmodule,
                ExternalType,
                ExternalSubroutine,
                ExternalFunction,
                ExternalInterface,
                ExternalSubroutine,
                ExternalProgram,
                ExternalSourceFile,
            ),
        ):
            self.fromstr = True
            obj = obj.name

        self.url = None
        if self.fromstr:
            m = HYPERLINK_RE.match(obj)
            if m:
                self.url = m.group(1)[1:-1]
                self.name = m.group(2)
            else:
                self.name = obj
            self.ident = self.name
        else:
            d = obj.get_dir()
            if not d:
                d = "none"
            self.ident = d + "~" + obj.ident
            self.name = obj.name
            m = EM_RE.search(self.name)
            if m:
                self.name = "<<i>" + m.group(1).strip() + "</i>>"
            self.url = obj.get_url()
        self.attribs["label"] = self.name
        if self.url and getattr(obj, "visible", True):
            if self.fromstr or hasattr(obj, "external_url"):
                self.attribs["URL"] = self.url
            else:
                self.attribs["URL"] = _parentdir + self.url
        self.afferent = 0
        self.efferent = 0

    def __eq__(self, other):
        return self.ident == other.ident

    def __lt__(self, other):
        return self.ident < other.ident

    def __hash__(self):
        # When making graphs in parallel, nodes might not have all
        # their attributes at some point?
        try:
            return hash(self.ident)
        except AttributeError:
            return id(self)


class ModNode(BaseNode):
    colour = "#337AB7"

    def __init__(self, obj, gd, hist=None):
        super().__init__(obj)
        self.uses = set()
        self.used_by = set()
        self.children = set()
        if self.fromstr:
            return
        for u in obj.uses:
            n = gd.get_node(u)
            n.used_by.add(self)
            n.afferent += 1
            self.uses.add(n)
            self.efferent += n.efferent


class SubmodNode(ModNode):
    colour = "#5bc0de"

    def __init__(self, obj, gd, hist=None):
        super().__init__(obj, gd)
        del self.used_by
        if self.fromstr:
            return
        if obj.parent_submodule:
            self.ancestor = gd.get_node(obj.parent_submodule)
        else:
            self.ancestor = gd.get_node(obj.ancestor_module)
        self.ancestor.children.add(self)
        self.efferent += 1
        self.ancestor.afferent += 1


class TypeNode(BaseNode):
    colour = "#5cb85c"

    def __init__(self, obj, gd, hist=None):
        super().__init__(obj)
        self.ancestor = None
        self.children = set()
        self.comp_types = dict()
        self.comp_of = dict()
        hist = hist or {}
        if self.fromstr:
            return
        if hasattr(obj, "external_url"):
            # Stop following chain, as this object is in an external project
            return

        if obj.extends:
            if obj.extends in hist:
                self.ancestor = hist[obj.extends]
            else:
                self.ancestor = gd.get_node(obj.extends, newdict(hist, obj, self))
            self.ancestor.children.add(self)
            self.ancestor.visible = getattr(obj.extends, "visible", True)

        for var in obj.local_variables:
            if var.vartype not in ["type", "class"]:
                continue

            proto = var.proto[0]
            if proto == "*":
                continue

            if proto == obj:
                node = self
            elif proto in hist:
                node = hist[proto]
            else:
                node = gd.get_node(proto, newdict(hist, obj, self))

            node.visible = getattr(proto, "visible", True)
            if self in node.comp_of:
                node.comp_of[self] += ", " + var.name
            else:
                node.comp_of[self] = var.name
            if node in self.comp_types:
                self.comp_types[node] += ", " + var.name
            else:
                self.comp_types[node] = var.name


class ProcNode(BaseNode):
    COLOURS = {"subroutine": "#d9534f", "function": "#d94e8f", "interface": "#A7506F"}

    @property
    def colour(self):
        return ProcNode.COLOURS.get(self.proctype.lower(), super().colour)

    def __init__(self, obj, gd, hist=None):
        # ToDo: Figure out appropriate way to handle interfaces to routines in submodules.
        self.proctype = getattr(obj, "proctype", "")
        super().__init__(obj)
        self.uses = set()
        self.calls = set()
        self.called_by = set()
        self.interfaces = set()
        self.interfaced_by = set()

        hist = hist or {}

        if self.fromstr:
            return
        for u in getattr(obj, "uses", []):
            n = gd.get_node(u)
            n.used_by.add(self)
            self.uses.add(n)
        for c in getattr(obj, "calls", []):
            if getattr(c, "visible", True):
                if c == obj:
                    n = self
                elif c in hist:
                    n = hist[c]
                else:
                    n = gd.get_node(c, newdict(hist, obj, self))
                n.called_by.add(self)
                self.calls.add(n)

        if obj.proctype.lower() != "interface":
            return

        for m in getattr(obj, "modprocs", []):
            if m.procedure and getattr(m.procedure, "visible", True):
                if m.procedure in hist:
                    n = hist[m.procedure]
                else:
                    n = gd.get_node(m.procedure, newdict(hist, obj, self))
                n.interfaced_by.add(self)
                self.interfaces.add(n)

        if (
            hasattr(obj, "procedure")
            and obj.procedure.module
            and obj.procedure.module is not True
            and getattr(obj.procedure.module, "visible", True)
        ):
            if obj.procedure.module in hist:
                n = hist[obj.procedure.module]
            else:
                n = gd.get_node(
                    obj.procedure.module,
                    newdict(hist, obj, self),
                )
            n.interfaced_by.add(self)
            self.interfaces.add(n)


class ProgNode(BaseNode):
    colour = "#f0ad4e"

    def __init__(self, obj, gd, hist=None):
        super().__init__(obj)
        self.uses = set()
        self.calls = set()
        if self.fromstr:
            return
        for u in obj.uses:
            usee = u
            if isinstance(u, str):
                usee = ExternalModule(u)
            n = gd.get_node(usee)
            n.used_by.add(self)
            self.uses.add(n)
        for c in obj.calls:
            if not getattr(c, "visible", False):
                continue
            callee = c
            if isinstance(c, str):
                callee = ExternalSubmodule(c)
            n = gd.get_node(callee)
            n.called_by.add(self)
            self.calls.add(n)


class BlockNode(BaseNode):
    colour = "#5cb85c"

    def __init__(self, obj, gd, hist=None):
        super().__init__(obj)
        self.uses = set()
        if self.fromstr:
            return
        for u in obj.uses:
            n = gd.get_node(u)
            n.used_by.add(self)
            self.uses.add(n)


class FileNode(BaseNode):
    colour = "#f0ad4e"

    def __init__(self, obj, gd, hist=None):
        super().__init__(obj)
        self.afferent = set()  # Things depending on this file
        self.efferent = set()  # Things this file depends on
        hist = hist or {}
        if self.fromstr:
            return

        for mod in itertools.chain(
            obj.modules,
            obj.submodules,
            obj.functions,
            obj.subroutines,
            obj.programs,
            obj.blockdata,
        ):
            for dep in mod.deplist:
                if dep.hierarchy[0] == obj:
                    continue
                if dep.hierarchy[0] in hist:
                    n = hist[dep.hierarchy[0]]
                else:
                    n = gd.get_node(
                        dep.hierarchy[0],
                        newdict(hist, obj, self),
                    )
                n.afferent.add(self)
                self.efferent.add(n)


def _edge(tail, head, style, colour, label=None):
    return {
        "tail_name": tail.ident,
        "head_name": head.ident,
        "style": style,
        "color": colour,
        "label": label,
    }


def _solid_edge(tail, head, colour, label=None):
    return _edge(tail, head, "solid", colour, label)


def _dashed_edge(tail, head, colour, label=None):
    return _edge(tail, head, "dashed", colour, label)


if graphviz_installed:
    # Create the legends for the graphs. These are their own separate graphs,
    # without edges
    gd = GraphData()

    # Graph nodes for a bunch of fake entities that we'll use in the legend
    _module = gd.get_node(ExternalModule("Module"))
    _submodule = gd.get_node(ExternalSubmodule("Submodule"))
    _type = gd.get_node(ExternalType("Type"))
    _subroutine = gd.get_node(ExternalSubroutine("Subroutine"))
    _function = gd.get_node(ExternalFunction("Function"))
    _interface = gd.get_node(ExternalInterface("Interface"))
    _unknown_proc = ExternalSubroutine("Unknown Procedure Type")
    _unknown_proc.proctype = "Unknown"
    _unknown = gd.get_node(_unknown_proc)
    _program = gd.get_node(ExternalProgram("Program"))
    _sourcefile = gd.get_node(ExternalSourceFile("Source File"))

    def _make_legend(entities):
        """Make a legend containing a collection of entities"""
        dot = Digraph(
            "Graph Key",
            graph_attr={"size": "8.90625,1000.0", "concentrate": "false"},
            node_attr={
                "shape": "box",
                "height": "0.0",
                "margin": "0.08",
                "fontname": "Helvetica",
                "fontsize": "10.5",
            },
            edge_attr={"fontname": "Helvetica", "fontsize": "9.5"},
            format="svg",
            engine="dot",
        )
        for entity in entities:
            dot.node(entity.name, **entity.attribs)
        dot.node("This Page's Entity")
        return dot.pipe().decode("utf-8")

    mod_svg = _make_legend([_module, _submodule, _subroutine, _function, _program])
    type_svg = _make_legend([_type])
    call_svg = _make_legend([_subroutine, _function, _interface, _unknown, _program])
    file_svg = _make_legend([_sourcefile])
else:
    mod_svg = ""
    type_svg = ""
    call_svg = ""
    file_svg = ""

NODE_DIAGRAM = "<p>Nodes of different colours represent the following: </p>"

MOD_GRAPH_KEY = f"""
{NODE_DIAGRAM}
{mod_svg}
<p>Solid arrows point from a submodule to the (sub)module which it is
descended from. Dashed arrows point from a module or program unit to 
modules which it uses.
</p>
"""  # noqa W291

TYPE_GRAPH_KEY = f"""
{NODE_DIAGRAM}
{type_svg}
<p>Solid arrows point from a derived type to the parent type which it
extends. Dashed arrows point from a derived type to the other
types it contains as a components, with a label listing the name(s) of
said component(s).
</p>
"""

CALL_GRAPH_KEY = f"""
{NODE_DIAGRAM}
{call_svg}
<p>Solid arrows point from a procedure to one which it calls. Dashed 
arrows point from an interface to procedures which implement that interface.
This could include the module procedures in a generic interface or the
implementation in a submodule of an interface in a parent module.
</p>
"""  # noqa W291

FILE_GRAPH_KEY = f"""
{NODE_DIAGRAM}
{file_svg}
<p>Solid arrows point from a file to a file which it depends on. A file
is dependent upon another if the latter must be compiled before the former
can be.
</p>
"""

COLOURED_NOTICE = """Where possible, edges connecting nodes are
given different colours to make them easier to distinguish in
large graphs."""

del call_svg
del file_svg
del type_svg
del mod_svg


class FortranGraph:
    """
    Object used to construct the graph for some particular entity in the code.
    """

    RANKDIR = "RL"
    _should_add_nested_nodes = False
    legend = ""

    def __init__(self, root, data: GraphData, webdir="", ident=None):
        """
        Initialize the graph, root is the object or list of objects,
        for which the graph is to be constructed.
        The webdir is the url where the graph should be stored, and
        ident can be provided to override the default identifacation
        of the graph that will be used to construct the name of the
        imagefile. It has to be provided if there are multiple root
        nodes.
        """
        self.root = []  # root nodes
        self.data = data
        self.hop_nodes = []  # nodes of the hop which exceeded the maximum
        self.hop_edges = []  # edges of the hop which exceeded the maximum
        self.added = set()  # nodes added to the graph
        self.max_nesting = 0  # maximum numbers of hops allowed
        self.max_nodes = 1  # maximum numbers of nodes allowed
        self.warn = False  # should warnings be written?
        self.truncated = -1  # nesting where the graph was truncated

        if not isinstance(root, (set, list)):
            root = [root]

        for r in sorted(root):
            self.root.append(self.data.get_node(r))
            self.max_nesting = max(self.max_nesting, int(r.meta["graph_maxdepth"]))
            self.max_nodes = max(self.max_nodes, int(r.meta["graph_maxnodes"]))
            self.warn = self.warn or (r.settings["warn"])

        self.webdir = webdir
        ident = ident or f"{root[0].get_dir()}~~{root[0].ident}"
        self.ident = f"{ident}~~{self.__class__.__name__}"
        self.imgfile = self.ident
        self.dot = Digraph(
            self.ident,
            graph_attr={
                "size": "8.90625,1000.0",
                "rankdir": self.RANKDIR,
                "concentrate": "true",
                "id": self.ident,
            },
            node_attr={
                "shape": "box",
                "height": "0.0",
                "margin": "0.08",
                "fontname": "Helvetica",
                "fontsize": "10.5",
            },
            edge_attr={"fontname": "Helvetica", "fontsize": "9.5"},
            format="svg",
            engine="dot",
        )
        # add root nodes to the graph
        for n in self.root:
            if len(self.root) == 1:
                self.dot.node(n.ident, label=n.name)
            else:
                self.dot.node(n.ident, **n.attribs)
            self.added.add(n)
        # add nodes and edges depending on the root nodes to the graph
        self.add_nodes(self.root)

        if graphviz_installed:
            self.svg_src = self.dot.pipe().decode("utf-8")
            self.svg_src = self.svg_src.replace(
                "<svg ", '<svg id="' + re.sub(r"[^\w]", "", self.ident) + '" '
            )
            w = int(WIDTH_RE.search(self.svg_src).group(1))
            if isinstance(self, (ModuleGraph, CallGraph, TypeGraph)):
                self.scaled = w >= 855
            else:
                self.scaled = w >= 641
        else:
            self.svg_src = ""
            self.scaled = False

    def add_to_graph(self, nodes, edges, nesting):
        """
        Adds nodes and edges to the graph as long as the maximum number
        of nodes is not exceeded.
        All edges are expected to have a reference to an entry in nodes.
        If the list of nodes is not added in the first hop due to graph
        size limitations, they are stored in hop_nodes.
        If the graph was extended the function returns True, otherwise the
        result will be False.
        """
        if (len(nodes) + len(self.added)) > self.max_nodes:
            if nesting < 2:
                self.hop_nodes = nodes
                self.hop_edges = edges
            self.truncated = nesting
            return False

        for n in sorted(nodes):
            strattribs = {key: str(a) for key, a in n.attribs.items()}
            self.dot.node(n.ident, **strattribs)
        for edge in edges:
            self.dot.edge(**edge)
        self.added.update(nodes)
        return True

    def __str__(self):
        """
        The string of the graph is its HTML representation.
        It will only be created if it is not too large.
        If the graph is overly large but can represented by a single node
        with many dependencies it will be shown as a table instead to ease
        the rendering in browsers.
        """

        graph_as_table = len(self.hop_nodes) > 0 and len(self.root) == 1

        # Do not render empty graphs
        if len(self.added) <= 1 and not graph_as_table:
            return ""

        # Do not render overly large graphs.
        if len(self.added) > self.max_nodes and self.warn:
            warnings.warn(
                f"Warning: Not showing graph {self.ident} as it would exceed the maximal number of {self.max_nodes} nodes"
            )
            return ""
        # Do not render incomplete graphs.
        if len(self.added) < len(self.root) and self.warn:
            warnings.warn(
                f"Warning: Not showing graph {self.ident} as it would be incomplete"
            )
            return ""

        if self.truncated > 0 and self.warn:
            warnings.warn(
                f"Warning: Graph {self.ident} is truncated after {self.truncated} hops"
            )

        rettext = ""
        if graph_as_table:
            # generate a table graph if maximum number of nodes gets exceeded in
            # the first hop and there is only one root node.
            root = f'<td class="root" rowspan="{len(self.hop_nodes) * 2 + 1}">{self.root[0].attribs["label"]}</td>'
            if self.hop_edges[0][0].ident == self.root[0].ident:
                key = 1
                root_on_left = self.RANKDIR == "LR"
                if root_on_left:
                    arrowtemp = '<td class="{0}{1}">{2}</td><td rowspan="2" class="triangle-right"></td>'
                else:
                    arrowtemp = '<td rowspan="2" class="triangle-left"></td><td class="{0}{1}">{2}</td>'
            else:
                key = 0
                root_on_left = self.RANKDIR == "RL"
                if root_on_left:
                    arrowtemp = '<td rowspan="2" class="triangle-left"></td><td class="{0}{1}">{2}</td>'
                else:
                    arrowtemp = '<td class="{0}{1}">{2}</td><td rowspan="2" class="triangle-right"></td>'
            # sort nodes in alphabetical order
            self.hop_edges.sort(key=lambda x: x[key].attribs["label"].lower())
            rows = ""
            for e in self.hop_edges:
                n = e[key]
                if len(e) == 5:
                    arrow = arrowtemp.format(e[2], "Text", e[4])
                else:
                    arrow = arrowtemp.format(e[2], "Bottom", "w")
                node = f'<td rowspan="2" class="node" bgcolor="{n.attribs["color"]}">'
                try:
                    node += (
                        f'<a href="{n.attribs["URL"]}">{n.attribs["label"]}</a></td>'
                    )
                except KeyError:
                    node += n.attribs["label"] + "</td>"

                root_arrow = (
                    f"{root}{arrow}{node}" if root_on_left else f"{node}{arrow}{root}"
                )
                rows += f"<tr>{root_arrow}</tr>\n"
                rows += f'<tr><td class="{e[2]}Top">w</td></tr>\n'
                root = ""
            rettext += f'<table class="graph">\n{rows}</table>\n'

        # generate svg graph
        else:
            rettext += f'<div class="depgraph">{self.svg_src}</div>'
            # add zoom ability for big graphs
            if self.scaled:
                zoomName = re.sub(r"[^\w]", "", self.ident)
                rettext += f"""\
                <script>
                  var pan{zoomName} = svgPanZoom('#{zoomName}',
                    {{zoomEnabled: true, controlIconsEnabled: true, fit: true, center: true,}}
                  );
                </script>"""

        legend_graph = f"""\
        <div><a type="button" class="graph-help" data-toggle="modal" href="#graph-help-text">Help</a></div>
          <div class="modal fade" id="graph-help-text" tabindex="-1" role="dialog">
            <div class="modal-dialog modal-lg" role="document">
              <div class="modal-content">
                <div class="modal-header">
                  <button type="button" class="close" data-dismiss="modal" aria-label="Close">
                    <span aria-hidden="true">&times;</span>
                  </button>
                  <h4 class="modal-title" id="-graph-help-label">Graph Key</h4>
                </div>
              <div class="modal-body">{self.legend} {COLOURED_NOTICE if _coloured_edges else ""}</div>
            </div>
          </div>
        </div>"""
        return rettext + legend_graph

    def __nonzero__(self):
        return self.__bool__()

    def __bool__(self):
        return bool(self.__str__())

    def create_svg(self, out_location: pathlib.Path):
        if len(self.added) > len(self.root):
            out_location = pathlib.Path(out_location)
            self._create_image_file(out_location / self.imgfile)

    def _create_image_file(self, filename: pathlib.Path):
        if not graphviz_installed:
            return

        self.dot.render(str(filename), cleanup=False)
        filename.rename(str(filename) + ".gv")

    def add_nodes(self, nodes, nesting=1):
        """Add nodes and edges to this graph, based on the collection ``nodes``

        Subclasses should implement `_add_node`, and optionally
        `_extra_attributes`

        """
        hop_nodes = set()  # nodes in this hop
        hop_edges = []  # edges in this hop

        total_len = len(nodes)

        for i, node in enumerate(sorted(nodes)):
            colour = rainbowcolour(i, total_len)

            self._add_node(hop_nodes, hop_edges, node, colour)

        if not self.add_to_graph(hop_nodes, hop_edges, nesting):
            return

        self._extra_attributes()

        if self._should_add_nested_nodes:
            self._add_nested_nodes(hop_nodes, nesting)

    def _add_nested_nodes(self, hop_nodes, nesting):
        """Handles nested nodes"""
        if len(hop_nodes) == 0:
            return

        if nesting < self.max_nesting:
            self.add_nodes(hop_nodes, nesting=nesting + 1)
        else:
            self.truncated = nesting

    def _add_node(self, hop_nodes, hop_edges, node, colour):
        """Add a single node and its edges to this graph, typically by
        iterating over parents/children

        """

        raise NotImplementedError

    def _extra_attributes(self):
        """Add any extra attributes to the graph"""
        pass


class ModuleGraph(FortranGraph):
    """Shows the relationship between modules and submodules"""

    legend = MOD_GRAPH_KEY

    def _add_node(self, hop_nodes, hop_edges, node, colour):
        for nu in sorted(node.uses):
            if nu not in self.added:
                hop_nodes.add(nu)
            hop_edges.append(_dashed_edge(node, nu, colour))

        if hasattr(node, "ancestor"):
            if node.ancestor not in self.added:
                hop_nodes.add(node.ancestor)
            hop_edges.append(_solid_edge(node, node.ancestor, colour))

    def _extra_attributes(self):
        self.dot.attr("graph", size="11.875,1000.0")


class UsesGraph(FortranGraph):
    """Graphs how modules use other modules, including ancestor (sub)modules"""

    _should_add_nested_nodes = True
    legend = MOD_GRAPH_KEY

    def _add_node(self, hop_nodes, hop_edges, node, colour):
        for nu in sorted(node.uses):
            if nu not in self.added:
                hop_nodes.add(nu)
            hop_edges.append(_dashed_edge(node, nu, colour))

        if hasattr(node, "ancestor"):
            if node.ancestor not in self.added:
                hop_nodes.add(node.ancestor)
            hop_edges.append(_solid_edge(node, node.ancestor, colour))


class UsedByGraph(FortranGraph):
    """Graphs how modules are used by other modules"""

    _should_add_nested_nodes = True
    legend = MOD_GRAPH_KEY

    def _add_node(self, hop_nodes, hop_edges, node, colour):
        for nu in sorted(getattr(node, "used_by", [])):
            if nu not in self.added:
                hop_nodes.add(nu)
            hop_edges.append(_dashed_edge(nu, node, colour))
        for c in sorted(getattr(node, "children", [])):
            if c not in self.added:
                hop_nodes.add(c)
            hop_edges.append(_solid_edge(c, node, colour))


class FileGraph(FortranGraph):
    """Graphs relationships between source files"""

    legend = FILE_GRAPH_KEY

    def _add_node(self, hop_nodes, hop_edges, node, colour):
        for ne in sorted(node.efferent):
            if ne not in self.added:
                hop_nodes.add(ne)
            hop_edges.append(_solid_edge(ne, node, colour))


class EfferentGraph(FortranGraph):
    """Shows the relationship between the files which this one depends on"""

    _should_add_nested_nodes = True
    legend = FILE_GRAPH_KEY

    def _add_node(self, hop_nodes, hop_edges, node, colour):
        for ne in sorted(node.efferent):
            if ne not in self.added:
                hop_nodes.add(ne)
            hop_edges.append(_dashed_edge(node, ne, colour))


class AfferentGraph(FortranGraph):
    """Shows the relationship between files which depend upon this one"""

    _should_add_nested_nodes = True
    legend = FILE_GRAPH_KEY

    def _add_node(self, hop_nodes, hop_edges, node, colour):
        for na in sorted(node.afferent):
            if na not in self.added:
                hop_nodes.add(na)
            hop_edges.append(_dashed_edge(na, node, colour))


class TypeGraph(FortranGraph):
    """Graphs inheritance and composition relationships between derived types"""

    legend = TYPE_GRAPH_KEY

    def _add_node(self, hop_nodes, hop_edges, node, colour):
        for keys in node.comp_types.keys():
            if keys not in self.added:
                hop_nodes.add(keys)
        for c in node.comp_types:
            if c not in self.added:
                hop_nodes.add(c)
            hop_edges.append(_dashed_edge(node, c, colour, node.comp_types[c]))
        if node.ancestor:
            if node.ancestor not in self.added:
                hop_nodes.add(node.ancestor)
            hop_edges.append(_solid_edge(node, node.ancestor, colour))

    def _extra_attributes(self):
        self.dot.attr("graph", size="11.875,1000.0")


class InheritsGraph(FortranGraph):
    """Graphs types that this type inherits from"""

    _should_add_nested_nodes = True
    legend = TYPE_GRAPH_KEY

    def _add_node(self, hop_nodes, hop_edges, node, colour):
        for c in node.comp_types:
            if c not in self.added:
                hop_nodes.add(c)
            hop_edges.append(_dashed_edge(node, c, colour, node.comp_types[c]))
        if node.ancestor:
            if node.ancestor not in self.added:
                hop_nodes.add(node.ancestor)
            hop_edges.append(_solid_edge(node, node.ancestor, colour))


class InheritedByGraph(FortranGraph):
    """Graphs types that inherit this type"""

    _should_add_nested_nodes = True
    legend = TYPE_GRAPH_KEY

    def _add_node(self, hop_nodes, hop_edges, node, colour):
        for c in node.comp_of:
            if c not in self.added:
                hop_nodes.add(c)
            hop_edges.append(_dashed_edge(c, node, colour, node.comp_of[c]))
        for c in node.children:
            if c not in self.added:
                hop_nodes.add(c)
            hop_edges.append(_solid_edge(c, node, colour))


class CallGraph(FortranGraph):
    """
    Adds edges indicating the call-tree for the procedures listed in
    the nodes.
    """

    RANKDIR = "LR"
    legend = CALL_GRAPH_KEY

    def _add_node(self, hop_nodes, hop_edges, node, colour):
        for p in sorted(node.calls):
            if p not in hop_nodes:
                hop_nodes.add(p)
            hop_edges.append(_solid_edge(node, p, colour))
        for p in sorted(getattr(node, "interfaces", [])):
            if p not in hop_nodes:
                hop_nodes.add(p)
            hop_edges.append(_dashed_edge(node, p, colour))

    def _extra_attributes(self):
        self.dot.attr("graph", size="11.875,1000.0")
        self.dot.attr("graph", concentrate="false")


class CallsGraph(FortranGraph):
    """Graphs procedures that this procedure calls"""

    RANKDIR = "LR"
    _should_add_nested_nodes = True
    legend = CALL_GRAPH_KEY

    def _add_node(self, hop_nodes, hop_edges, node, colour):
        for p in sorted(node.calls):
            if p not in self.added:
                hop_nodes.add(p)
            hop_edges.append(_solid_edge(node, p, colour))
        for p in sorted(getattr(node, "interfaces", [])):
            if p not in self.added:
                hop_nodes.add(p)
            hop_edges.append(_dashed_edge(node, p, colour))

    def _extra_attributes(self):
        self.dot.attr("graph", concentrate="false")


class CalledByGraph(FortranGraph):
    """Graphs procedures called by this procedure"""

    RANKDIR = "LR"
    _should_add_nested_nodes = True
    legend = CALL_GRAPH_KEY

    def _add_node(self, hop_nodes, hop_edges, node, colour):
        if isinstance(node, ProgNode):
            return
        for p in sorted(node.called_by):
            if p not in self.added:
                hop_nodes.add(p)
            hop_edges.append(_solid_edge(p, node, colour))
        for p in sorted(getattr(node, "interfaced_by", [])):
            if p not in self.added:
                hop_nodes.add(p)
            hop_edges.append(_dashed_edge(p, node, colour))

    def _extra_attributes(self):
        self.dot.attr("graph", concentrate="false")


class BadType(Exception):
    """
    Raised when a type is passed to GraphData.register() which is not
    accepted.
    """

    def __init__(self, value):
        self.value = value

    def __str__(self):
        return repr(self.value)


def outputFuncWrap(args):
    """Wrapper function for output graphs -- needed to allow multiprocessing to
    pickle the function (must be at top level)"""

    for f in args[0:-1]:
        f.create_svg(args[-1])

    return None


class GraphManager(object):
    """
    Object which contains graphs of module use relations, type relations,
    call trees, etc. It manages these, ensures that everything that is
    needed is added at the correct time, and produces the plots for the
    list pages.

      base_url
        The URL at which the documentation will be stored. If using
        relative URLs then should be '..'.
      outdir
        The directory in which the documentation will be produced.
      graphdir:
        The location of the graphs within the output tree.
    """

    def __init__(
        self,
        base_url: os.PathLike,
        outdir: os.PathLike,
        graphdir: os.PathLike,
        parentdir: os.PathLike,
        coloured_edges: bool,
        save_graphs: bool = False,
    ):
        self.graph_objs = []
        self.modules = set()
        self.programs = set()
        self.procedures = set()
        self.types = set()
        self.sourcefiles = set()
        self.blockdata = set()
        self.save_graphs = save_graphs
        self.graphdir = pathlib.Path(graphdir)
        self.webdir = pathlib.Path(base_url) / self.graphdir
        self.usegraph = None
        self.typegraph = None
        self.callgraph = None
        self.filegraph = None
        self.data = GraphData()
        set_coloured_edges(coloured_edges)
        set_graphs_parentdir(parentdir)

    def register(self, obj):
        if obj.meta["graph"]:
            self.data.register(obj)
            self.graph_objs.append(obj)

    def graph_all(self):
        for obj in tqdm(sorted(self.graph_objs), unit="", desc="Generating graphs"):
            if is_module(obj):
                obj.usesgraph = UsesGraph(obj, self.data, self.webdir)
                obj.usedbygraph = UsedByGraph(obj, self.data, self.webdir)
                self.modules.add(obj)
            elif is_type(obj):
                obj.inhergraph = InheritsGraph(obj, self.data, self.webdir)
                obj.inherbygraph = InheritedByGraph(obj, self.data, self.webdir)
                self.types.add(obj)
            elif is_proc(obj):
                obj.callsgraph = CallsGraph(obj, self.data, self.webdir)
                obj.calledbygraph = CalledByGraph(obj, self.data, self.webdir)
                obj.usesgraph = UsesGraph(obj, self.data, self.webdir)
                self.procedures.add(obj)
            elif is_program(obj):
                obj.usesgraph = UsesGraph(obj, self.data, self.webdir)
                obj.callsgraph = CallsGraph(obj, self.data, self.webdir)
                self.programs.add(obj)
            elif is_sourcefile(obj):
                obj.afferentgraph = AfferentGraph(obj, self.data, self.webdir)
                obj.efferentgraph = EfferentGraph(obj, self.data, self.webdir)
                self.sourcefiles.add(obj)
            elif is_blockdata(obj):
                obj.usesgraph = UsesGraph(obj, self.data, self.webdir)
                self.blockdata.add(obj)

        usenodes = sorted(list(self.modules))
        callnodes = sorted(list(self.procedures))
        for p in sorted(self.programs):
            if len(p.usesgraph.added) > 1:
                usenodes.append(p)
            if len(p.callsgraph.added) > 1:
                callnodes.append(p)
        for p in sorted(self.procedures):
            if len(p.usesgraph.added) > 1:
                usenodes.append(p)
        for b in self.blockdata:
            if len(b.usesgraph.added) > 1:
                usenodes.append(b)
        self.usegraph = ModuleGraph(usenodes, self.data, self.webdir, "module~~graph")
        self.typegraph = TypeGraph(self.types, self.data, self.webdir, "type~~graph")
        self.callgraph = CallGraph(callnodes, self.data, self.webdir, "call~~graph")
        self.filegraph = FileGraph(
            self.sourcefiles, self.data, self.webdir, "file~~graph"
        )

    def output_graphs(self, njobs=0):
        if not self.save_graphs:
            return

        self.graphdir.mkdir(exist_ok=True, parents=True, mode=0o755)

        if njobs == 0:
            for m in self.modules:
                m.usesgraph.create_svg(self.graphdir)
                m.usedbygraph.create_svg(self.graphdir)
            for t in self.types:
                t.inhergraph.create_svg(self.graphdir)
                t.inherbygraph.create_svg(self.graphdir)
            for p in self.procedures:
                p.callsgraph.create_svg(self.graphdir)
                p.calledbygraph.create_svg(self.graphdir)
            for p in self.programs:
                p.callsgraph.create_svg(self.graphdir)
                p.usesgraph.create_svg(self.graphdir)
            for f in self.sourcefiles:
                f.afferentgraph.create_svg(self.graphdir)
                f.efferentgraph.create_svg(self.graphdir)
            for b in self.blockdata:
                b.usesgraph.create_svg(self.graphdir)
        else:
            args = []
            # Note we generate all graphs for a given object in one wrapper call
            # this is to try to ensure we don't get name collisions not present
            # in the serial version (e.g. due to calling usesgraph and usedbygraph on
            # a particular module in two different processes). May not actually be needed
            # commented block above allows testing of one graph per call approach.
            args.extend(
                [(m.usesgraph, m.usedbygraph, self.graphdir) for m in self.modules]
            )
            args.extend(
                [(m.inhergraph, m.inherbygraph, self.graphdir) for m in self.types]
            )
            args.extend(
                [
                    (m.callsgraph, m.calledbygraph, self.graphdir)
                    for m in self.procedures
                ]
            )
            args.extend(
                [(m.callsgraph, m.usesgraph, self.graphdir) for m in self.programs]
            )
            args.extend(
                [
                    (m.afferentgraph, m.efferentgraph, self.graphdir)
                    for m in self.sourcefiles
                ]
            )
            args.extend([(m.usesgraph, self.graphdir) for m in self.blockdata])

            process_map(
                outputFuncWrap,
                args,
                max_workers=njobs,
                desc="Writing graphs",
            )

        for graph in [self.usegraph, self.typegraph, self.callgraph, self.filegraph]:
            if graph:
                graph.create_svg(self.graphdir)
