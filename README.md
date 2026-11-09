# Easy Adjust

Easy Adjust adds a Houdini-inspired value ladder and customizable hover shortcuts to editable numeric fields in Blender.

## Value ladder

1. Hold the middle mouse button over an editable numeric field.
2. Move vertically inside the ladder to select a step size. Choosing a step keeps the value in place.
3. Move past the left or right edge to lock the step and begin adjusting the value.
4. Release the middle mouse button to confirm. Press Esc or right-click to cancel.

The default steps are `100`, `10`, `1`, `0.1`, `0.01`, and `0.001`, in the field's displayed units. You can choose 1–12 steps and edit each step size in the add-on preferences. Reducing the count hides the extra values and keeps them for later. Duplicate values are merged and the displayed steps are sorted from largest to smallest.

The ladder adjusts the current numeric component. Blender's native left-button dragging and keyboard input provide multi-field editing.

## Hover shortcuts

Pause over an editable numeric field for about half a second to show four shortcut buttons. Their default operations are divide by 4, divide by 2, multiply by 1.5, and multiply by 2. In the current package, the corresponding default labels are `−1`, `−½`, `+½`, and `+1`.

Each button's operation, value, and label can be customized. The preferences also provide controls for hover delay, drag sensitivity, popup size, and enabling each tool separately.

## Installation

In Blender, open **Edit → Preferences → Get Extensions** and choose **Install from Disk** from the upper-right menu. Select `easy_adjust-1.1.2.zip` and enable **Easy Adjust**.

The package declares Blender 4.2.7 or later. The current release has been tested on Windows with Blender 5.2.3 LTS. Some third-party custom controls may be unsupported.

## Support

Use [GitHub Issues](https://github.com/youyooo/easy-adjust/issues) to report problems, ask questions, or suggest improvements. Include your Blender version, operating system, steps to reproduce the problem, and the location of the numeric field. A small example file or a screenshot can help reproduce a problem.

## License

GNU General Public License version 3 or later (`GPL-3.0-or-later`).
