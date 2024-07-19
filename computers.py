from PIL import Image
import numpy as np

def extract_and_print_average_values(image_path):

    big_image = Image.open(image_path)

    image4 = big_image.crop((3*512, 0, 4*512, 512))
    image5 = big_image.crop((4*512, 0, 5*512, 512))

    image4_array = np.array(image4, dtype=np.float32)
    image5_array = np.array(image5, dtype=np.float32)

    average_value_image4 = np.mean(image4_array)/255
    average_value_image5 = np.mean(image5_array)/255

    print(f"Average of roughness: {average_value_image4}")
    print(f"Average of specular: {average_value_image5}")

extract_and_print_average_values('/mnt/iusers01/fatpou01/compsci01/v67771bx/dataset/0000255_combined.png')
