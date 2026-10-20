package example;

public class OrderService {
    private final PriceService priceService = new PriceService();

    public int create(int price) {
        return priceService.calculate(price);
    }
}
