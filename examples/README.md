# Example images and SVG overlays

The `images/` folder contains sample BMP images and their matching SVG overlays,
named `sample-001.bmp` / `sample-001.svg`, and so on in the original filename order.
Keep each image and SVG together with the same filename stem so ClassifyImage
can find the overlay automatically.

Launch ClassifyImage and browse to `examples/images/` to try overlay rendering,
line and text sizing, zooming, and SVG anchor selection. Lock an anchor and
navigate between the images to check how the selected anchor follows each image.

These files are also available as sample data for integration tests. Copy them
to a temporary folder before testing classification transfers so the examples
stay intact.
