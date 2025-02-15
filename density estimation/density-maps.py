import os

import matplotlib.pyplot as plt
import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter

# from pycocotools.coco import COCO


def read_yolo_annotations(annotation_file, image_width, image_height):
    """
    Read YOLO format annotations and convert to pixel coordinates.

    :param annotation_file: Path to YOLO annotation file
    :param image_width: Width of the image
    :param image_height: Height of the image
    :return: List of (x, y, class) tuples
    """
    points = []
    with open(annotation_file, "r") as f:
        for line in f:
            class_id, x_center, y_center, _, _ = map(float, line.strip().split())
            x = int(x_center * image_width)
            y = int(y_center * image_height)
            points.append((x, y, int(class_id)))
    return points


def create_density_map(image_shape, points, sigma=10, min_value=1e-4):
    """
    Create a density map from point annotations.

    :param image_shape: Tuple of (height, width) of the image
    :param points: List of (x, y, class) tuples
    :param sigma: Standard deviation for Gaussian kernel
    :return: Density map as a 2D numpy array
    """
    density_map = np.zeros(image_shape, dtype=np.float32)

    for x, y, _ in points:
        density_map[y, x] = 100

    density_map = gaussian_filter(density_map, sigma=sigma, mode="constant")

    # Clip the Gaussian output
    density_map = np.clip(density_map, a_min=min_value, a_max=None)

    # Normalize the map and scale it up to the number of points * 100
    density_map = density_map / density_map.sum() * len(points) * 100

    return density_map


def create_class_specific_density_maps(image_shape, points, class_labels, sigma=10):
    """
    Create separate density maps for each class.

    :param image_shape: Tuple of (height, width) of the image
    :param points: List of (x, y, class) tuples
    :param num_classes: Number of classes in the dataset
    :param sigma: Standard deviation for Gaussian kernel
    :return: List of density maps, one for each class
    """
    class_density_maps = []

    for class_id in class_labels:
        class_points = [(x, y, c) for x, y, c in points if c == class_id]
        # print(class_points)
        class_map = create_density_map(image_shape, class_points, sigma)
        class_density_maps.append(class_map)

    return class_density_maps


def resize_image(image, target_size):
    original_width, original_height = image.size
    aspect_ratio = original_width / original_height
    target_width, target_height = target_size

    if aspect_ratio > target_width / target_height:
        new_width = target_width
        new_height = int(new_width / aspect_ratio)
    else:
        new_height = target_height
        new_width = int(new_height * aspect_ratio)

    resized_image = image.resize((new_width, new_height), Image.LANCZOS)

    new_image = Image.new("RGB", target_size, (0, 0, 0))
    paste_x = (target_width - new_width) // 2
    paste_y = (target_height - new_height) // 2
    new_image.paste(resized_image, (paste_x, paste_y))

    return new_image, new_width, new_height, paste_x, paste_y


def process_images(
    image_folder,
    annotation_folder,
    output_map_folder,
    output_image_folder,
    class_labels,
    resize,
    target_size=(256, 256),
    sigma=10,
):
    os.makedirs(output_map_folder, exist_ok=True)
    os.makedirs(output_image_folder, exist_ok=True)

    for filename in os.listdir(image_folder):
        if filename.lower().endswith((".png", ".jpg", ".jpeg")):
            image_path = os.path.join(image_folder, filename)
            annotation_path = os.path.join(
                annotation_folder, os.path.splitext(filename)[0] + ".txt"
            )

            if not os.path.exists(annotation_path):
                print(f"Annotation file not found for {filename}, skipping.")
                continue

            print(f"Processing {filename}")

            # Load image
            original_image = Image.open(image_path)
            original_width, original_height = original_image.size

            if resize:
                # Resize image if resize is True
                resized_image, new_width, new_height, paste_x, paste_y = resize_image(
                    original_image, target_size
                )
                # Read YOLO annotations and adjust for resized image
                points = read_yolo_annotations(
                    annotation_path, original_width, original_height
                )
                # Adjust point coordinates for resized image
                scale_x = new_width / original_width
                scale_y = new_height / original_height
                adjusted_points = [
                    (int(x * scale_x) + paste_x, int(y * scale_y) + paste_y, c)
                    for x, y, c in points
                ]
                # Save resized image
                resized_image_path = os.path.join(output_image_folder, filename)
                resized_image.save(resized_image_path)
                image_shape = target_size
            else:
                # Use original image and points if resize is False
                adjusted_points = read_yolo_annotations(
                    annotation_path, original_width, original_height
                )
                # Copy original image to output folder
                original_image_path = os.path.join(output_image_folder, filename)
                original_image.save(original_image_path)
                image_shape = (original_height, original_width)

            # Create class-specific density maps
            class_density_maps = create_class_specific_density_maps(
                image_shape, adjusted_points, class_labels, sigma
            )

            # Save density maps
            for i, class_map in enumerate(class_density_maps):
                np.save(
                    os.path.join(
                        output_map_folder,
                        f"{os.path.splitext(filename)[0]}_class_{i}_density.npy",
                    ),
                    class_map,
                )

            # print(f"Processed {filename}")


def visualize_density_map(image_path, density_map_path, output_path=None):
    """
    Visualize the original image and its corresponding density map side by side.

    :param image_path: path to the original image
    :param density_map_path: path to the density map
    :param output_path: path to save the visualization
    """

    image = Image.open(image_path)
    # print(image)
    # Load the density map
    density_map = np.load(density_map_path)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(20, 10))

    # Display original image
    ax1.imshow(image)
    ax1.set_title("Original Image")
    ax1.axis("off")

    # Display density map
    im = ax2.imshow(density_map, cmap="jet")
    ax2.set_title("Density Map")
    ax2.axis("off")

    # Add colorbar
    plt.colorbar(im, ax=ax2)

    plt.tight_layout()
    # plt.savefig(output_path)
    # plt.close()


if __name__ == "__main__":
    stub_list = ["train", "val", "test"]
    target_size = (512, 512)
    sigma = 12
    resize = False

    for stub in stub_list:
        if resize:
            target_size_str = f"{target_size[0]}x{target_size[1]}"
        else:
            target_size_str = "original_size_dmx100"

        output_image_folder = (
            f"../datasets/corn_kernel_density/{stub}/{target_size_str}/sigma-{sigma}/"
        )
        output_map_folder = (
            f"../datasets/corn_kernel_density/{stub}/{target_size_str}/sigma-{sigma}/"
        )

        image_folder = f"../datasets/corn_kernel_yolo/images/{stub}/"
        annotation_folder = f"../datasets/corn_kernel_yolo/labels/{stub}/"

        class_labels = [0]  # 0 for kernel

        process_images(
            image_folder,
            annotation_folder,
            output_map_folder,
            output_image_folder,
            class_labels,
            resize,
            target_size,
            sigma,
        )

        # Visualize the density map for a sample (first) image in the dataset
        image_files = [
            file
            for file in os.listdir(output_image_folder)
            if file.lower().endswith(".jpg")
        ]

        if image_files:
            first_image_file = image_files[0]

            image_path = os.path.join(output_image_folder, first_image_file)
            img_name = os.path.splitext(first_image_file)[0]
            kernel_density_map_path = os.path.join(
                output_map_folder, f"{img_name}_class_0_density.npy"
            )

            visualize_density_map(image_path, kernel_density_map_path, output_path=None)
        else:
            print("No files found in the output_image_folder.")
