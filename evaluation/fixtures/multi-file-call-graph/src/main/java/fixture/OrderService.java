package fixture;

public class OrderService {
    private final PriceService priceService = new PriceService();

    public int total(int price) {
        return priceService.doublePrice(price);
    }
}
