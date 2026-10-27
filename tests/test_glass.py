"""The glass drawing: smooth rounded shapes stretched to any size, and the canvas-drawn controls."""
import tkinter as tk
import unittest
from types import SimpleNamespace

from dubchecker import glass
from dubchecker.theme import apply_theme
from tests.test_gui import tk_available


def fake_app(root: tk.Tk) -> SimpleNamespace:
    """Just what the widgets need from the main window (and the theme's fonts)."""
    return SimpleNamespace(palette=apply_theme(root, "light"), scale=1.0, px=lambda v: round(v), surfaces=[])


def pixel(image: tk.PhotoImage, x: int, y: int) -> str:
    return "#%02x%02x%02x" % image.get(x, y)


class ColourTests(unittest.TestCase):
    def test_mix(self) -> None:
        self.assertEqual(glass.mix("#000000", "#ffffff", 0.5), "#808080")
        self.assertEqual(glass.mix("#123456", "#abcdef", 0), "#123456")
        self.assertEqual(glass.mix("#123456", "#abcdef", 1), "#abcdef")


@unittest.skipUnless(tk_available(), "no display for Tk")
class ShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self) -> None:
        self.root.destroy()

    def test_stretched_shape_keeps_round_corners_and_a_solid_middle(self) -> None:
        look = glass.Look(8, "#ff0000", "#ffffff")
        image = glass.stretched(self.root, look, 120, 40)
        self.assertEqual((image.width(), image.height()), (120, 40))
        self.assertEqual(pixel(image, 0, 0), "#ffffff")       # outside the rounded corner
        self.assertEqual(pixel(image, 119, 39), "#ffffff")
        self.assertEqual(pixel(image, 60, 20), "#ff0000")     # the middle
        self.assertEqual(pixel(image, 60, 0), "#ff0000")      # a straight edge reaches the border
        corner = pixel(image, 1, 3)  # on the curve
        self.assertNotIn(corner, ("#ffffff", "#ff0000"))      # smoothed, not jagged

    def test_shadow_and_rim(self) -> None:
        look = glass.Look(6, "#eeeeee", "#ffffff", "#ffffff", "#999999", shadow=0.3, blur=4, drop=2)
        self.assertEqual(look.margin, 6)
        image = glass.stretched(self.root, look, 80, 50)
        below = pixel(image, 40, 50 - 3)  # under the shape, in its shadow
        self.assertLess(int(below[1:3], 16), 255)
        self.assertEqual(pixel(image, 40, 50 - 7), "#999999")  # the bottom edge's colour
        self.assertEqual(glass.template(self.root, look), glass.template(self.root, look))  # drawn once

    def test_tiny_sizes_grow_to_the_smallest_shape(self) -> None:
        image = glass.stretched(self.root, glass.Look(5, "#000000", "#ffffff"), 2, 2)
        self.assertEqual((image.width(), image.height()), (11, 11))


@unittest.skipUnless(tk_available(), "no display for Tk")
class ControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = fake_app(self.root)

    def tearDown(self) -> None:
        self.root.destroy()

    def test_pill_button(self) -> None:
        clicks = []
        button = glass.PillButton(self.root, self.app, "Scan library", lambda: clicks.append(1), accent=True)
        self.assertEqual(button.cget("text"), "Scan library")
        button.invoke()
        button.state(("disabled",))
        self.assertIn("disabled", button.state())
        button.invoke()  # does nothing while disabled
        button.state(("!disabled",))
        self.assertNotIn("disabled", button.state())
        self.assertEqual(clicks, [1])
        width = int(button.cget("width"))
        button.configure(text="Scan library again")
        self.assertEqual(button.cget("text"), "Scan library again")
        self.assertGreater(int(button.cget("width")), width)
        self.assertIn(button, self.app.surfaces)

    def test_a_press_that_slides_off_is_not_a_click(self) -> None:
        clicks = []
        button = glass.PillButton(self.root, self.app, "Go", lambda: clicks.append(1))
        button.pack()
        self.root.update()
        button._on_press(SimpleNamespace(x=5, y=5))
        button._on_release(SimpleNamespace(x=-50, y=5))
        self.assertEqual(clicks, [])
        button._on_press(SimpleNamespace(x=5, y=5))
        button._on_release(SimpleNamespace(x=6, y=6))
        self.assertEqual(clicks, [1])

    def test_segmented_control(self) -> None:
        variable = tk.StringVar(value="folder")
        changed = []
        control = glass.SegmentedControl(self.root, self.app, variable, [("folder", "Local files"),
                                                                        ("sonarr", "Sonarr")], changed.append)
        control.command = lambda: changed.append(variable.get())
        self.assertEqual([s.cget("text") for s in control.segments], ["Local files", "Sonarr"])
        control.segments[1].invoke()
        self.assertEqual((variable.get(), changed), ("sonarr", ["sonarr"]))
        control.segments[0].state(("disabled",))
        control.segments[0].invoke()
        self.assertEqual(variable.get(), "sonarr")  # locked while disabled

    def test_search_field_placeholder(self) -> None:
        variable = tk.StringVar()
        field = glass.SearchField(self.root, self.app, variable, width=200)
        field.redraw()
        self.assertEqual(field.itemcget(field._placeholder, "state"), "normal")
        self.assertEqual(field.itemcget(field._window, "state"), "hidden")
        variable.set("gintama")
        self.assertEqual(field.itemcget(field._placeholder, "state"), "hidden")
        self.assertEqual(field.itemcget(field._window, "state"), "normal")
        self.assertEqual(field.entry.get(), "gintama")


if __name__ == "__main__":
    unittest.main()
