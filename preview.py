import matplotlib.pyplot as plt
from astropy.visualization import simple_norm


def plot_cutout(cutout, size_arcmin):
    """Display one cutout for local inspection."""
    fig = plt.figure(figsize=(8, 7))
    ax = fig.add_subplot(111, projection=cutout["wcs"])

    norm = simple_norm(
        cutout["image"],
        stretch="asinh",
        percent=99,
    )

    plot = ax.imshow(
        cutout["image"],
        origin="lower",
        cmap="gray",
        norm=norm,
    )

    ax.plot(
        cutout["target_x"],
        cutout["target_y"],
        marker="+",
        color="red",
        markersize=14,
    )

    ax.set_xlabel("Right ascension")
    ax.set_ylabel("Declination")
    ax.set_title(
        f"SPHEREx: requested {size_arcmin:g} × {size_arcmin:g} arcmin\n"
        f"{cutout['date_utc']} UTC | {cutout['detector']}"
    )

    fig.colorbar(
        plot,
        ax=ax,
        label=f"Surface brightness ({cutout['brightness_unit']})",
    )

    plt.show()