package fixture;

public class ContentPipeline {
    private final SlugService slugs = new SlugService();
    private final DisplayService display = new DisplayService();

    public String slug(String value) { return slugs.slug(value); }
    public String display(String value) { return display.display(value); }
}
